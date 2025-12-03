# [FEATURE NAME] Scope

> **VERTICAL SLICE REQUIREMENT**: This scope defines a standalone, demo-able feature.
> It includes everything needed: database tables, relationships, UI composition, and business logic.
> A developer should be able to build and demo this feature using only this document.

---

## 1. Overview & Objectives

### 1.1 Problem Statement
<!-- What problem are we solving? -->

### 1.2 Goals & Success Metrics
<!-- What does success look like? How will we measure it? -->

### 1.3 Demo Scenario
<!-- Describe what the demo will look like when this feature is complete. Walk through the user flow. -->

### 1.4 Scope In / Scope Out
| In Scope | Out of Scope |
|----------|--------------|
|          |              |

---

## 2. Personas & User Stories

### 2.1 Personas
<!-- Who are the key users? -->

### 2.2 User Stories

#### BOQ Import
- [ ] As a [persona], I want to [action] so that [benefit]

#### Rate Maintenance
- [ ] As a [persona], I want to [action] so that [benefit]

#### Tender Build-up
- [ ] As a [persona], I want to [action] so that [benefit]

#### Approvals
- [ ] As a [persona], I want to [action] so that [benefit]

#### Reporting
- [ ] As a [persona], I want to [action] so that [benefit]

---

## 3. Current (As-Is) Process

### 3.1 Narrative & Diagrams
<!-- Describe current workflow, include diagrams if available -->

### 3.2 Key Pain Points
1.
2.
3.

### 3.3 Inventory of Current Spreadsheets
| Spreadsheet Name | Purpose | Owner | Issues |
|------------------|---------|-------|--------|
|                  |         |       |        |

---

## 4. Future (To-Be) Process

### 4.1 Step-by-Step Flows
1.
2.
3.

### 4.2 Swimlanes by Role
<!-- Role-based process diagram -->

---

## 5. Database Schema (REQUIRED)

> **This section is MANDATORY.** Define every table, column, and relationship needed for this feature.

### 5.1 Table Definitions

#### Table: [table_name]
| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | bigint | PK, auto-increment | Primary key |
| [column] | [type] | [constraints] | [description] |
| created_at | datetime | NOT NULL | |
| updated_at | datetime | NOT NULL | |

<!-- Repeat for each table in this feature -->

### 5.2 Relationships & Cardinality

| Parent Table | Child Table | Relationship | Foreign Key | On Delete |
|--------------|-------------|--------------|-------------|-----------|
| [parent] | [child] | 1:many | [fk_column] | cascade/nullify/restrict |

### 5.3 Entity Relationship Diagram
```
[Parent Table]
    |
    |-- 1:many --> [Child Table]
                       |
                       |-- 1:many --> [Grandchild Table]
```

### 5.4 Indexes
| Table | Index Name | Columns | Type | Purpose |
|-------|------------|---------|------|---------|
|       |            |         | btree/unique | |

---

## 6. UI Composition & Scaffolding (REQUIRED)

> **This section is MANDATORY.** Define how tables compose in the UI as nested/related views.

### 6.1 Screen Hierarchy
```
[Main List View: Parent Table]
    |
    +-- [Detail View: Parent Record]
            |
            +-- [Nested Table: Child Records]
                    |
                    +-- [Inline Edit / Modal: Grandchild Records]
```

### 6.2 View Specifications

#### View: [View Name]
- **Route**: `/[resource]/[action]`
- **Primary Table**: [table_name]
- **Displays**: [list columns shown]
- **Actions**: [create, edit, delete, etc.]
- **Nested Components**:
  - [Child table displayed as]: [table/cards/accordion]
  - [Relationship]: Parent has_many :children

### 6.3 UI Composition Rules
| Parent View | Child Component | Display Style | Interaction |
|-------------|-----------------|---------------|-------------|
| [parent] index | [child] list | nested table | inline add/edit |
| [parent] show | [child] cards | card grid | modal edit |

---

## 7. Calculations & Business Rules

### 7.1 Ephemeral Calculations
<!-- Formulas, pseudo-code, Excel references -->

### 7.2 Business Rules Summary
| Rule ID | Description | Trigger | Action |
|---------|-------------|---------|--------|
| BR-001  |             |         |        |
| BR-002  |             |         |        |

### 7.3 Edge Cases
- **Zero quantity**:
- **Missing rate**:
- **Negative adjustment**:

---

## 8. Outputs & Reporting

### 8.1 Output Tables
<!-- Summary tables, calculated results -->

### 8.2 Documents & Artifacts
- Tender PDFs
- Internal cost reports
- Exports

### 8.3 Sample Layouts / Mockups
<!-- Screenshots, wireframes, example outputs -->

---

## 9. Roles, Permissions, and Audit

### 9.1 Role Matrix
| Role | Create | Read | Update | Delete | Approve |
|------|--------|------|--------|--------|---------|
|      |        |      |        |        |         |

### 9.2 What's Logged / Versioned
-

### 9.3 When Snapshots Are Taken
-

---

## 10. Open Questions, Risks, Assumptions

### 10.1 Open Questions
- [ ] Q1:
- [ ] Q2:

### 10.2 Risks
| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
|      |            |        |            |

### 10.3 Assumptions
1.
2.

---

## 11. Sprint Task Generation Notes

> This section is used to generate implementation tasks that follow our Rails/Hotwire conventions.
> Each component uses: Turbo Frames, always-editable forms, dirty-form controller, instant "+ Add" creation.

### 11.1 Component Build Order (Leaf First)

> Build and test bottom-up: leaf nodes first, then parents. Each component is independently testable.

| Order | Model | Route to Test | Parent Model | Has Children? |
|-------|-------|---------------|--------------|---------------|
| 1 | [leaf_model] | /[plural]/1 | [parent] | No |
| 2 | [parent_model] | /[plural]/1 | [grandparent] | Yes: [children] |
| 3 | [root_model] | /[plural]/1 | None | Yes: [children] |

### 11.2 Per-Component Task Template

For each model in the build order, create these files:

```
app/views/[plural]/
  show.html.erb              # just: <%= render @record %>
  _[model_name].html.erb     # editable Turbo Frame component

app/controllers/[plural]_controller.rb
  # show, update, create (with Turbo Stream response)

app/javascript/controllers/
  [model-name]_controller.js  # only if calculations needed
```

### 11.3 Fields Per Component

| Model | Editable Fields | Calculated Fields | Child Collection |
|-------|-----------------|-------------------|------------------|
| [model] | [field1, field2] | [calc1 = formula] | [children] |

### 11.4 Stimulus Controllers Needed

| Controller | Purpose | Models Using It |
|------------|---------|-----------------|
| dirty-form | Unsaved changes indicator | All components |
| [model-name] | [specific calculation] | [model] |

### 11.5 Seed Data Requirements

| Model | # Records | Key Test Scenarios |
|-------|-----------|-------------------|
| [model] | 2-3 | [describe test cases] |

### 11.6 Dependencies
<!-- List any dependencies on existing models, controllers, or shared components -->
