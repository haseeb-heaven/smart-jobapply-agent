---
name: domain-modeling
description: >-
  Build and refine a project's domain model by extracting entities, value objects,
  aggregates, and relationships from business requirements. Creates shared terminology
  that bridges stakeholders and developers. Use when designing new features, refactoring
  complex domains, or onboarding team members to unfamiliar codebases.
---

# Domain Modeling

## Smart JobApply Agent boundary

Model `CandidateEvidence`, `JobListing`, `EligibilityDecision`,
`ScoreExplanation`, and `AuditRecord`. The model documents language and
invariants; it does not replace runtime policy, professional-evidence labels,
eligibility-before-ranking, or matcher tests.

Build and refine a project's domain model — extracting the key concepts, rules, and relationships that define the problem space.

## When NOT to Use

- Simple CRUD apps with straightforward data models — don't over-engineer
- UI/UX design discussions — those use different modeling tools (wireframes, user flows)
- Infrastructure/architecture planning — separate concern from domain logic

## Core Building Blocks

| Element | What It Is | Example |
|---------|-----------|---------|
| **Entity** | Has unique identity, mutable state | `User`, `Order` |
| **Value Object** | Defined by its attributes, immutable | `Money`, `EmailAddress` |
| **Aggregate** | Cluster of related entities/value objects with a boundary | `Order` + `LineItem` + `ShippingAddress` |
| **Repository** | Abstraction over data persistence | `orderRepo.findById(...)` |
| **Service** | Orchestrates domain operations across aggregates | `checkout(order, payment)` |
| **Domain Event** | Something meaningful that happened | `OrderPlaced`, `PaymentFailed` |

## The Modeling Process

### Step 1: Ubiquitous Language
Gather terms from stakeholder conversations, documentation, and existing code. List every noun and verb. Disagreements about terminology reveal domain complexity you need to understand.

### Step 2: Identify Aggregates
Group related entities into aggregates based on:
- **Consistency boundary** — These must always be correct together
- **Transaction boundary** — These change atomically
- **Access pattern** — These are typically loaded/saved as a unit

### Step 3: Define Relationships
| Relationship | Description | Implementation |
|-------------|-------------|----------------|
| Composition | Strong "owns-a" relationship | Embedded / foreign key |
| Association | Weak "uses" relationship | Reference / pointer |
| Dependency | Temporal/informational link | Interface / event subscription |

### Step 4: Capture Invariants
Business rules that must ALWAYS be true:
- An order cannot have zero line items
- A user's balance cannot go negative without an approved credit memo
- Two users cannot have the same email address

Document these as code-level assertions, test cases, or database constraints.

### Step 5: Validate
Walk through concrete scenarios with domain stakeholders:
- "When a customer cancels an order that has already shipped..."
- "When inventory drops below threshold during checkout..."

If your model doesn't handle real scenarios, it needs refinement.

## Output Format

```markdown
# Domain Model: [Name]

## Key Entities
- **Entity**: Properties, invariants, lifecycle
- **Entity**: ...

## Value Objects
- **VO**: Immutable, equality-by-value

## Aggregates
- **Aggregate Root**: Boundary definition, consistency rules

## Relationships
- Entity → Aggregate: type, cardinality

## Business Rules (Invariants)
1. [Rule]: Always true

## Domain Events
- **Event**: Triggers, payload, handlers
```

**Source:** [mattpocock/skills](https://github.com/mattpocock/skills) — Skills for Real Engineers
