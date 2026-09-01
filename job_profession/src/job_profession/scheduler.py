"""Local, idempotent job-discovery/export scheduler.

The scheduler never opens a job board and never performs an application action.
It scores listing payloads supplied through :class:`VisiblePageAdapter` and
exports only high-confidence recommendations for a human to review.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import errno
from hashlib import sha256
import json
import logging
import math
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
import time
from typing import Any, Iterable, Mapping
from uuid import uuid4

try:  # POSIX advisory locks.
    import fcntl as _fcntl
except ModuleNotFoundError:  # pragma: no cover - exercised in an isolated child
    _fcntl = None

try:  # Windows advisory locks.
    import msvcrt as _msvcrt
except ModuleNotFoundError:  # pragma: no cover - platform dependent
    _msvcrt = None

from .matcher import matcher_policy_revision, score_job
from .models import CandidateProfile, JobListing
from .normalize import listing_fingerprint, normalize_listing
from .sources import SearchProfile, VisiblePageAdapter, _validate_listing_url, build_search_url, listing_from_visible_payload


LOGGER = logging.getLogger(__name__)
MINIMUM_RECOMMENDED_SCORE = 85
DEFAULT_LOCK_WAIT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.05
_LOCK_FILE_NAME = ".job_discovery_scheduler.lock"
_PROFILE_REVISION_SCHEMA_VERSION = 2
_PORTABLE_CLAIM_STALE_SECONDS = 24 * 60 * 60
_INCOMPLETE_CLAIM_GRACE_SECONDS = 1.0
_MAX_PORTABLE_CLAIM_BYTES = 16_384


def _process_is_running(pid: object) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno not in {errno.ESRCH, errno.EINVAL}
    return True


def _portable_claim_path(lock_path: Path) -> Path:
    return lock_path.with_name(lock_path.name + ".claim")


def _portable_recovery_path(lock_path: Path) -> Path:
    return lock_path.with_name(lock_path.name + ".claim.recovery")


@dataclass(frozen=True, slots=True)
class _PortableClaimSnapshot:
    claim: Mapping[str, Any] | None
    device: int
    inode: int
    modified_ns: int
    size: int
    digest: str

    @property
    def identity(self) -> tuple[int, int, int, int, str]:
        return (self.device, self.inode, self.modified_ns, self.size, self.digest)


def _read_portable_claim_snapshot(path: Path) -> _PortableClaimSnapshot | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_PORTABLE_CLAIM_BYTES + 1)
            metadata = os.fstat(handle.fileno())
    except OSError:
        return None
    value: object = None
    if len(raw) <= _MAX_PORTABLE_CLAIM_BYTES:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return _PortableClaimSnapshot(
        claim=value if isinstance(value, Mapping) else None,
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        modified_ns=int(metadata.st_mtime_ns),
        size=int(metadata.st_size),
        digest=sha256(raw).hexdigest(),
    )


def _portable_claim_is_stale(
    path: Path, *, snapshot: _PortableClaimSnapshot | None = None
) -> bool:
    snapshot = snapshot or _read_portable_claim_snapshot(path)
    if snapshot is None:
        return True
    claim = snapshot.claim
    age = max(0.0, time.time() - (snapshot.modified_ns / 1_000_000_000))
    if claim is None:
        return age >= _INCOMPLETE_CLAIM_GRACE_SECONDS
    acquired_at = claim.get("acquired_at")
    if isinstance(acquired_at, bool) or not isinstance(acquired_at, (int, float)):
        return age >= _INCOMPLETE_CLAIM_GRACE_SECONDS
    return not _process_is_running(claim.get("pid")) or time.time() - acquired_at >= _PORTABLE_CLAIM_STALE_SECONDS


def _unlink_portable_claim_if_unchanged(path: Path, expected: _PortableClaimSnapshot) -> bool:
    """Remove only the exact stale claim previously inspected.

    The inode, metadata, and content digest are all re-read immediately before
    removal. A contender that replaced or refreshed the path therefore cannot
    have its live claim removed by stale-recovery work based on an older read.
    """

    current = _read_portable_claim_snapshot(path)
    if current is None or current.identity != expected.identity:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _try_portable_claim(lock_path: Path, token: str) -> bool:
    claim_path = _portable_claim_path(lock_path)
    recovery_path = _portable_recovery_path(lock_path)
    recovery_snapshot = _read_portable_claim_snapshot(recovery_path)
    if recovery_snapshot is not None:
        if _portable_claim_is_stale(recovery_path, snapshot=recovery_snapshot):
            _unlink_portable_claim_if_unchanged(recovery_path, recovery_snapshot)
        return False
    payload = json.dumps(
        {"schema_version": 1, "pid": os.getpid(), "token": token, "acquired_at": time.time()},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        descriptor = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        snapshot = _read_portable_claim_snapshot(claim_path)
        if snapshot is not None and _portable_claim_is_stale(claim_path, snapshot=snapshot):
            recovery_token = str(uuid4())
            recovery_payload = json.dumps(
                {
                    "schema_version": 1,
                    "pid": os.getpid(),
                    "token": recovery_token,
                    "acquired_at": time.time(),
                    "expected_claim_digest": snapshot.digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                recovery_descriptor = os.open(
                    recovery_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError:
                return False
            try:
                os.write(recovery_descriptor, recovery_payload)
                os.fsync(recovery_descriptor)
            finally:
                os.close(recovery_descriptor)
            try:
                # Every conforming claimant observes the recovery marker before
                # creating a claim. Only this elected recovery owner may remove
                # the stale identity, closing the inspect/unlink replacement race.
                _unlink_portable_claim_if_unchanged(claim_path, snapshot)
            finally:
                marker = _read_portable_claim_snapshot(recovery_path)
                if marker is not None and marker.claim is not None and marker.claim.get("token") == recovery_token:
                    _unlink_portable_claim_if_unchanged(recovery_path, marker)
        return False
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    except OSError:
        try:
            claim_path.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)
    return True


def _release_portable_claim(lock_path: Path, token: str) -> None:
    claim_path = _portable_claim_path(lock_path)
    snapshot = _read_portable_claim_snapshot(claim_path)
    if snapshot is not None and snapshot.claim is not None and snapshot.claim.get("token") == token:
        _unlink_portable_claim_if_unchanged(claim_path, snapshot)


@dataclass(frozen=True, slots=True)
class DiscoveryRunResult:
    """Auditable outcome of one export-only discovery run."""

    run_id: str
    started_at: str
    finished_at: str
    search_urls: tuple[str, ...]
    payloads_seen: int
    new_listings: int
    duplicate_listings: int
    recommended_exports: int
    below_threshold: int
    malformed_payloads: int
    errors: tuple[str, ...]
    profile_revision: str
    matcher_policy_revision: str
    application_actions: int = 0
    minimum_profile_fit_score: int = MINIMUM_RECOMMENDED_SCORE

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["search_urls"] = list(self.search_urls)
        data["errors"] = list(self.errors)
        data["mode"] = "discovery_export_only"
        data["network_access"] = "none_by_scheduler"
        return data


def candidate_profile_revision(profile: CandidateProfile, *, rules_path: str | Path | None = None) -> str:
    """Return a deterministic hash of only safe matching evidence.

    Contact details, compensation, and autofill values are intentionally
    absent. The selected fields are approved evidence and normalized
    eligibility constraints, including work-authorization classifications,
    that can change the discovery decision.
    """

    def normalized(values: Iterable[str]) -> list[str]:
        return sorted({str(value).casefold().strip() for value in values if str(value).strip()})

    evidence_labels = {"professional", "personal_open_source", "learning_or_exposure"}
    evidence = sorted(
        (str(skill).casefold().strip(), label)
        for skill, label in profile.evidence_by_skill.items()
        if str(skill).strip() and label in evidence_labels
    )
    projection = {
        "schema_version": _PROFILE_REVISION_SCHEMA_VERSION,
        "professional_skills": normalized(profile.professional_skills),
        "personal_open_source_skills": normalized(profile.personal_open_source_skills),
        "learning_or_exposure_skills": normalized(profile.learning_or_exposure_skills),
        "years_experience": profile.years_experience,
        "role_targets": normalized(profile.role_targets),
        "excluded_title_terms": normalized(profile.excluded_title_terms),
        "mandatory_excluded_requirements": normalized(profile.mandatory_excluded_requirements),
        "location_preferences": normalized(profile.location_preferences),
        "work_mode_preferences": normalized(profile.work_mode_preferences),
        "employment_type_preferences": normalized(profile.employment_type_preferences),
        "work_authorizations": normalized(profile.work_authorizations),
        "evidence_by_skill": evidence,
        "matcher_policy_revision": matcher_policy_revision(str(rules_path) if rules_path is not None else None),
    }
    encoded = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def _has_safe_exported_listing_identity(value: Mapping[str, Any]) -> bool:
    """Validate an exported review link before showing it in the active queue."""

    fingerprint = value.get("fingerprint")
    platform = value.get("platform")
    url = value.get("url")
    if (
        not isinstance(fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        or platform not in {"linkedin", "indeed"}
        or not isinstance(url, str)
    ):
        return False
    try:
        _validate_listing_url(url, platform)
    except ValueError:
        return False
    return True


def current_profile_recommendations(
    profile: CandidateProfile, export_path: str | Path, *, rules_path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Return only safe review rows exported for the active evidence revision.

    Recommendation exports are append-only audit records.  Consequently, a
    previous profile revision may have assessed the same listing differently.
    This read-only view deliberately excludes those historical rows (and
    legacy rows without a revision) instead of mutating or deleting them.
    """

    path = Path(export_path)
    if not path.exists():
        return []
    profile_revision = candidate_profile_revision(profile, rules_path=rules_path)
    active_policy_revision = matcher_policy_revision(str(rules_path) if rules_path is not None else None)
    recommendations: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, Mapping):
                    raise ValueError(f"Invalid discovery export row in {path} line {line_number}")
                if value.get("profile_revision") != profile_revision:
                    continue
                application_actions = value.get("application_actions")
                score = value.get("score")
                minimum_score = value.get("minimum_profile_fit_score")
                if (
                    value.get("record_type") != "recommended_job_for_human_review"
                    or value.get("discovery_mode") != "export_only"
                    or value.get("decision") != "recommended"
                    or not isinstance(application_actions, int)
                    or isinstance(application_actions, bool)
                    or application_actions != 0
                    or not isinstance(score, int)
                    or isinstance(score, bool)
                    or score < MINIMUM_RECOMMENDED_SCORE
                    or not isinstance(minimum_score, int)
                    or isinstance(minimum_score, bool)
                    or minimum_score != MINIMUM_RECOMMENDED_SCORE
                    or value.get("threshold_met") is not True
                    or value.get("matcher_policy_revision") != active_policy_revision
                    or value.get("platform") not in {"linkedin", "indeed"}
                    or value.get("is_test_fixture") is True
                    or not _has_safe_exported_listing_identity(value)
                ):
                    continue
                recommendations.append(dict(value))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid discovery export file {path}: {exc}") from exc
    return recommendations


class JobDiscoveryScheduler:
    """Run transparent discovery against an injected visible-page adapter only."""

    def __init__(
        self,
        profile: CandidateProfile,
        search_profiles: Iterable[SearchProfile],
        *,
        state_path: str | Path,
        export_path: str | Path,
        run_log_path: str | Path | None = None,
        minimum_score: int = MINIMUM_RECOMMENDED_SCORE,
        lock_wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
        rules_path: str | Path | None = None,
    ) -> None:
        if minimum_score != MINIMUM_RECOMMENDED_SCORE:
            raise ValueError("Discovery exports use the fixed 85-point profile-fit threshold")
        try:
            normalized_lock_wait_seconds = float(lock_wait_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("lock_wait_seconds must be a finite non-negative number") from exc
        if not math.isfinite(normalized_lock_wait_seconds) or normalized_lock_wait_seconds < 0:
            raise ValueError("lock_wait_seconds must be a finite non-negative number")
        self.profile = profile
        self.search_profiles = tuple(search_profiles)
        self.state_path = Path(state_path)
        self.export_path = Path(export_path)
        self.run_log_path = Path(run_log_path) if run_log_path else None
        self.minimum_score = minimum_score
        self.rules_path = Path(rules_path) if rules_path is not None else None
        artifact_paths = (self.state_path, self.export_path, *(() if self.run_log_path is None else (self.run_log_path,)))
        self._lock_paths = tuple(
            sorted(
                {
                    path.resolve(strict=False).parent / f".{path.resolve(strict=False).name}{_LOCK_FILE_NAME}"
                    for path in artifact_paths
                },
                key=str,
            )
        )
        self._lock_wait_seconds = normalized_lock_wait_seconds

    def run(self, adapter: VisiblePageAdapter, *, now: datetime | None = None) -> DiscoveryRunResult:
        """Score supplied visible listings, deduplicate, and export safe review rows.

        `adapter` is purposefully required.  There is no fallback implementation
        that can fetch LinkedIn/Indeed, access browser data, authenticate, or
        invoke an application flow.
        """

        with self._exclusive_run_lock():
            return self._run_locked(adapter, now=now)

    def _run_locked(self, adapter: VisiblePageAdapter, *, now: datetime | None = None) -> DiscoveryRunResult:
        """Execute one discovery run while holding its artifact locks."""

        timestamp = now or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        started_at = timestamp.isoformat()
        run_id = str(uuid4())
        rules_path = str(self.rules_path) if self.rules_path is not None else None
        policy_revision = matcher_policy_revision(rules_path)
        profile_revision = candidate_profile_revision(self.profile, rules_path=rules_path)
        state = self._read_state()
        known = set(state["fingerprints_by_profile_revision"].get(profile_revision, ()))
        exported = self._read_exported_fingerprints().get(profile_revision, set())
        run_seen: set[str] = set()
        state_candidates = set(known)
        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        payloads_seen = duplicates = new_listings = below_threshold = malformed = 0
        urls = tuple(dict.fromkeys(build_search_url(search) for search in self.search_profiles))

        for search in self.search_profiles:
            url = build_search_url(search)
            try:
                payloads = tuple(adapter.read_visible_listings(url))
            except Exception as error:  # The run remains an auditable no-action result.
                errors.append(f"{search.platform} adapter read failed: {type(error).__name__}")
                continue
            for payload in payloads:
                payloads_seen += 1
                if not isinstance(payload, Mapping):
                    malformed += 1
                    errors.append(f"{search.platform} visible payload was not a mapping")
                    continue
                try:
                    listing = listing_from_visible_payload(payload, platform=search.platform)
                    listing = normalize_listing(listing)
                except (TypeError, ValueError) as error:
                    malformed += 1
                    errors.append(f"{search.platform} visible payload rejected: {type(error).__name__}")
                    continue
                fingerprint = listing_fingerprint(listing.platform, listing.url, listing.title, listing.company)
                if fingerprint in known or fingerprint in run_seen:
                    duplicates += 1
                    continue
                run_seen.add(fingerprint)
                if fingerprint in exported:
                    # The recommendation row was durably written during an
                    # earlier interrupted run.  It is safe to commit it to
                    # state after this run's audit output succeeds, but must
                    # never be appended a second time.
                    duplicates += 1
                    continue
                new_listings += 1
                state_candidates.add(fingerprint)
                match = score_job(self.profile, listing, rules_path=rules_path)
                if match.decision != "recommended" or match.score < self.minimum_score:
                    below_threshold += 1
                    continue
                rows.append(
                    self._export_row(
                        listing, match, fingerprint, profile_revision, policy_revision, run_id, started_at, url
                    )
                )

        # Existing output is itself durable evidence that its listed jobs were
        # exported.  This closes the recovery path if an earlier run wrote the
        # append-only output but failed before it could commit state.
        state_candidates.update(exported)
        state["fingerprints_by_profile_revision"][profile_revision] = sorted(state_candidates)
        all_fingerprints = set(state["fingerprints"])
        all_fingerprints.update(state_candidates)
        state["fingerprints"] = sorted(all_fingerprints)
        state["schema_version"] = 2
        result = DiscoveryRunResult(
            run_id=run_id,
            started_at=started_at,
            finished_at=timestamp.isoformat(),
            search_urls=urls,
            payloads_seen=payloads_seen,
            new_listings=new_listings,
            duplicate_listings=duplicates,
            recommended_exports=len(rows),
            below_threshold=below_threshold,
            malformed_payloads=malformed,
            errors=tuple(errors),
            profile_revision=profile_revision,
            matcher_policy_revision=policy_revision,
        )
        # Commit ordering is deliberate: a fingerprint becomes seen only
        # after its review output and audit record have both been written.
        # If either write fails, state stays unchanged and a later run can
        # retry.  _read_exported_fingerprints prevents duplicate rows when an
        # earlier run completed the append but failed before state commit.
        self._append_json_lines(self.export_path, rows)
        self._log_result(result)
        self._atomic_json_write(self.state_path, state)
        return result

    @contextmanager
    def _exclusive_run_lock(self) -> Iterator[None]:
        """Serialize schedulers sharing any artifact with bounded locks.

        POSIX uses ``flock`` and Windows uses ``msvcrt.locking``. Environments
        without either API use an atomic ownership claim with PID and bounded
        stale-claim recovery; they never fall back to an unlocked run. Lock
        files are durable sentinels, while actual ownership is crash-recoverable.
        All artifact paths are acquired in stable order.
        """

        deadline = time.monotonic() + self._lock_wait_seconds
        handles: list[tuple[Path, Any, str, str | None]] = []
        try:
            for lock_path in self._lock_paths:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                handle = lock_path.open("a+", encoding="utf-8")
                backend = "fcntl" if _fcntl is not None else "msvcrt" if _msvcrt is not None else "claim"
                token = str(uuid4()) if backend == "claim" else None
                try:
                    while True:
                        blocked = False
                        try:
                            if backend == "fcntl":
                                assert _fcntl is not None
                                _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                            elif backend == "msvcrt":
                                assert _msvcrt is not None
                                handle.seek(0)
                                if handle.read(1) == "":
                                    handle.write("\0")
                                    handle.flush()
                                handle.seek(0)
                                _msvcrt.locking(handle.fileno(), _msvcrt.LK_NBLCK, 1)
                            else:
                                assert token is not None
                                blocked = not _try_portable_claim(lock_path, token)
                        except BlockingIOError:
                            blocked = True
                        except OSError:
                            if backend != "msvcrt":
                                raise
                            blocked = True
                        if not blocked:
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                f"Timed out waiting {self._lock_wait_seconds:g}s for scheduler locks"
                            )
                        time.sleep(min(_LOCK_POLL_SECONDS, remaining))
                except Exception:
                    handle.close()
                    raise
                handles.append((lock_path, handle, backend, token))
            yield
        finally:
            for lock_path, handle, backend, token in reversed(handles):
                try:
                    if backend == "fcntl" and _fcntl is not None:
                        _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
                    elif backend == "msvcrt" and _msvcrt is not None:
                        handle.seek(0)
                        _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
                    elif backend == "claim" and token is not None:
                        _release_portable_claim(lock_path, token)
                finally:
                    handle.close()

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema_version": 2, "fingerprints": [], "fingerprints_by_profile_revision": {}}
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid discovery state file {self.state_path}: {exc}") from exc
        fingerprints = value.get("fingerprints", []) if isinstance(value, dict) else []
        if not isinstance(fingerprints, list) or not all(isinstance(item, str) for item in fingerprints):
            raise ValueError(f"Invalid discovery state fingerprints in {self.state_path}")
        raw_by_revision = value.get("fingerprints_by_profile_revision", {}) if isinstance(value, dict) else {}
        if not isinstance(raw_by_revision, Mapping):
            raise ValueError(f"Invalid profile-revision discovery state in {self.state_path}")
        fingerprints_by_profile_revision: dict[str, list[str]] = {}
        for revision, revision_fingerprints in raw_by_revision.items():
            if not isinstance(revision, str) or not isinstance(revision_fingerprints, list):
                raise ValueError(f"Invalid profile-revision discovery state in {self.state_path}")
            if not all(isinstance(item, str) for item in revision_fingerprints):
                raise ValueError(f"Invalid profile-revision discovery state in {self.state_path}")
            fingerprints_by_profile_revision[revision] = revision_fingerprints
        return {
            "schema_version": value.get("schema_version", 1),
            "fingerprints": fingerprints,
            "fingerprints_by_profile_revision": fingerprints_by_profile_revision,
        }

    def _read_exported_fingerprints(self) -> dict[str, set[str]]:
        """Read durable recommendation identities for interrupted-run recovery.

        The scheduler owns this JSONL file.  Refusing a malformed line is
        safer than silently treating an uncertain export as durable and then
        permanently suppressing a listing.
        """

        if not self.export_path.exists():
            return {}
        fingerprints_by_profile_revision: dict[str, set[str]] = {}
        try:
            with self.export_path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    fingerprint = value.get("fingerprint") if isinstance(value, Mapping) else None
                    if not isinstance(fingerprint, str) or not fingerprint:
                        raise ValueError(
                            f"Invalid discovery export fingerprint in {self.export_path} line {line_number}"
                        )
                    profile_revision = value.get("profile_revision")
                    if profile_revision is None:
                        # Pre-revision rows are immutable but cannot safely
                        # suppress scoring for a newer evidence projection.
                        continue
                    if not isinstance(profile_revision, str) or not profile_revision:
                        raise ValueError(
                            f"Invalid discovery export profile revision in {self.export_path} line {line_number}"
                        )
                    fingerprints_by_profile_revision.setdefault(profile_revision, set()).add(fingerprint)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid discovery export file {self.export_path}: {exc}") from exc
        return fingerprints_by_profile_revision

    @staticmethod
    def _export_row(
        listing: JobListing,
        match: Any,
        fingerprint: str,
        profile_revision: str,
        matcher_policy_revision: str,
        run_id: str,
        discovered_at: str,
        search_url: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "record_type": "recommended_job_for_human_review",
            "discovery_mode": "export_only",
            "application_actions": 0,
            "fingerprint": fingerprint,
            "profile_revision": profile_revision,
            "matcher_policy_revision": matcher_policy_revision,
            "run_id": run_id,
            "discovered_at": discovered_at,
            "search_url": search_url,
            "platform": listing.platform,
            "title": listing.title,
            "company": listing.company,
            "url": listing.url,
            "location": listing.location,
            "work_mode": listing.work_mode,
            "posted_at": listing.posted_at,
            "score": match.score,
            "decision": match.decision,
            "minimum_profile_fit_score": MINIMUM_RECOMMENDED_SCORE,
            "threshold_met": match.score >= MINIMUM_RECOMMENDED_SCORE,
            "reasons": list(match.reasons),
            "gaps": list(match.gaps),
            "evidence_explanations": list(match.evidence_explanations),
            "score_explanation": match.score_explanation,
            "human_action_required": "Review listing and apply manually outside this tool if desired.",
        }

    @staticmethod
    def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            json.dump(value, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)

    @staticmethod
    def _append_json_lines(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
        rows = tuple(rows)
        if not rows:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _log_result(self, result: DiscoveryRunResult) -> None:
        message = json.dumps(result.as_dict(), sort_keys=True)
        LOGGER.info("job discovery run: %s", message)
        if self.run_log_path is not None:
            self.run_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.run_log_path.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")
                handle.flush()
                os.fsync(handle.fileno())


def run_discovery(
    profile: CandidateProfile,
    search_profiles: Iterable[SearchProfile],
    adapter: VisiblePageAdapter,
    *,
    state_path: str | Path,
    export_path: str | Path,
    run_log_path: str | Path | None = None,
    now: datetime | None = None,
    rules_path: str | Path | None = None,
) -> DiscoveryRunResult:
    """Convenience API for one explicit, adapter-only discovery/export run."""

    return JobDiscoveryScheduler(
        profile,
        search_profiles,
        state_path=state_path,
        export_path=export_path,
        run_log_path=run_log_path,
        rules_path=rules_path,
    ).run(adapter, now=now)
