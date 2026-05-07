# LU Reconciliation — Requirements Document
*FME 2024.2 PythonCaller — LU-Level Input Edition*

---

## 1. Overview

This script reconciles Logement Unit (LU) counts between a Deployment List (DL) and a Build List (BL) for Belgian fiber deployment. It runs as an FME 2024.2 PythonCaller using the class-based `FeatureProcessor` API (`input` / `close` / `pyoutput`).

Each incoming FME feature represents one individual LU row (LU-level input). All fields are scalar — no comma-separated multi-values. The script self-groups features by their MDU or Building Group (BG) identifier before applying reconciliation logic.

When ADD rows are needed, the script first attempts to fill slots with out-of-scope LUs belonging to the same group, before falling back to generating synthetic rows.

---

## 2. Input

### 2.1 Input ports

The PythonCaller exposes **two named input ports**:

| Port name | Content |
|---|---|
| `INPUT` | In-scope LU rows — the primary reconciliation dataset |
| `OOS_INPUT` | Out-of-scope LU rows — a pool of real LUs available for reuse when ADD slots arise |

Using two ports keeps the streams cleanly separated without requiring a flag field or upstream tagger. FME's `FeatureProcessor` supports multiple named input ports natively. The script routes incoming features to the correct internal bucket based on the port they arrive on.

### 2.2 Input attributes (both ports)

| Attribute | Type | Description |
|---|---|---|
| `MDU_ID` | String | MDU identifier. Used as grouping key when `BG_ID` is absent. |
| `BG_ID` | String / null | Building Group identifier. When populated, overrides `MDU_ID` as the grouping key and determines which count target to use. |
| `house_number` | String | Street house number. Passed through to output. |
| `house_number_extension` | String | House number extension (bus/bte). Passed through to output. |
| `jvid` | String | LU address key (Twin JVID reference). |
| `bus_number` | String / null | Apartment / unit identifier. |
| `status` | String / null | LU status (e.g. `HOMES_CONNECT`). |
| `delivery_status` | String / null | LU delivery status code. |
| `scopelist_v15` | String / null | Scope indicator. Populated (any value) = priority out-of-scope LU. Empty/null = not a priority OOS LU. Present on both ports; on `INPUT` features it is expected to be empty/null. |
| `MDU_Total` | Integer | Total target LU count at MDU level. Used when `BG_ID` is absent or null. |
| `BG_Total` | Integer | Total target LU count at Building Group level. Used when `BG_ID` is present. |
| `Delta` | Integer | Pre-computed difference (informational only). Not used directly in logic. |

### 2.3 Grouping key resolution

Each LU feature (from either port) is assigned to a group using the same rule:

| Condition | Grouping key | Target LU count source |
|---|---|---|
| `BG_ID` is present (non-null, non-empty) | `BG_ID` | `BG_Total` |
| `BG_ID` is absent or null, `MDU_ID` is present | `MDU_ID` | `MDU_Total` |
| Both `BG_ID` and `MDU_ID` are absent or null | *(ungroupable)* | — |

**Ungroupable LUs:** if a feature has neither `BG_ID` nor `MDU_ID`, it cannot be assigned to a group. All such LUs are passed through directly with `action = KEEP` and `feedback = "Kept: no grouping key (BG_ID and MDU_ID both absent)"`. No reconciliation logic is applied.

In-scope and out-of-scope LUs sharing the same resolved group key belong to the same building group and are processed together.

### 2.4 Target LU count

**Target LU count** = `BG_Total` (when `BG_ID` present) or `MDU_Total` (when `BG_ID` absent).

The **current LU count** for a group is the number of in-scope (`INPUT` port) LU features in that group only. Out-of-scope LUs do not count toward the current total.

---

## 3. Output

One FME feature per LU emitted, covering:
- All in-scope LUs (KEEP or REMOVE)
- All reused out-of-scope LUs (REUSE)
- All synthetic ADD rows (ADD, only when no OOS LUs remain)

| Attribute | Description |
|---|---|
| `MDU_ID` | Passed through from input. |
| `BG_ID` | Passed through from input (may be null). |
| `house_number` | Passed through from input. |
| `house_number_extension` | Passed through from input. |
| `jvid` | LU address key. Empty string for synthetic ADD rows. Populated for REUSE rows. |
| `bus_number` | Unit identifier. Algorithmically suggested for synthetic ADD rows (see §7). Carried from source for REUSE rows. |
| `status` | LU status. Empty string for synthetic ADD rows. |
| `delivery_status` | LU delivery status. Empty string for synthetic ADD rows. |
| `action` | `KEEP` / `REMOVE` / `ADD` / `REUSE` |
| `feedback` | Human-readable explanation of the action taken. |

---

## 4. Core Reconciliation Logic

### 4.1 Collect features

In `input(feature)`, store each incoming `FMEFeature` in an internal list, tagged with its source port. In `close()`, separate into two dicts keyed by resolved group key: one for in-scope LUs, one for out-of-scope LUs.

### 4.2 Compute delta

For each group, using in-scope LUs only:

- `current_LU_count` = number of in-scope LU rows in the group
- `n_remove` = `current_LU_count` − target
- `n_add` = target − `current_LU_count`
- If `n_remove = 0` and `n_add = 0` → all in-scope LUs receive `action = KEEP`

### 4.3 Pre-existing duplicate warning

Before applying remove / add logic, scan `bus_number` values within the in-scope LUs of a group. For any LU whose `bus_number` appears more than once, set feedback to:

> `"Warning: duplicate bus_number (X) in source data"`

This warning is preserved unless overwritten by a more specific REMOVE / KEEP / ADD / REUSE feedback later.

---

## 5. REMOVE Logic (`n_remove > 0`)

### 5.1 Lock — never removable

- Any LU with `status = HOMES_CONNECT` → locked, always KEEP.
- If `target = 1` (MDU becoming SDU): the first LU with an empty/null `bus_number` → locked, always KEEP.
  - Feedback: `"Kept: NULL bus_number retained (MDU becoming SDU)"`

### 5.2 Flag removal candidates (cumulative — all flags apply)

**Status-based flags** — apply only when statuses are not all alike across unlocked LUs:
- `delivery_status` starts with `"2"` → candidate; reason: `"delivery_status starts with 2"`
- `delivery_status` does not end with `"5"` (and is not empty) → candidate; reason: `"delivery_status not ending with 5"`

**Always-apply flags:**
- `bus_number` is empty/null AND there are multiple unlocked LUs → candidate; reason: `"bus_number is empty/null"`
- `bus_number` is a detected outlier (see §6) → candidate; reason: `"bus_number outlier (X)"`
- `bus_number` is a duplicate of one already seen in this group (second+ occurrence) → candidate; reason: `"duplicate bus_number (X)"`

### 5.3 Remove candidates first, then rest

- Sort candidates by `bus_number` in logical order (see §8); remove from the end first.
- If more removals are still needed after exhausting candidates, sort remaining unlocked LUs the same way and remove from the end.

| action | feedback |
|---|---|
| `REMOVE` | `"Remove: <reason1>, <reason2>, ..."` |
| `REMOVE` | `"Remove: removed from end (no candidate flags)"` |
| `KEEP` | `"Kept (candidate but not needed): <reasons>"` |

---

## 6. ADD Logic (`n_add > 0`)

All existing in-scope LUs → `action = KEEP`.

`n_add` slots must be filled. The script fills them in priority order:

### 6.1 Priority order for filling ADD slots

1. **Out-of-scope LUs with `scopelist_v15` populated** (any value) — highest priority
2. **Out-of-scope LUs with `scopelist_v15` empty/null** — second priority
3. **Synthetic ADD rows** (algorithmically generated) — only when OOS pool is exhausted

Within each OOS tier, check for `bus_number` duplicates against the in-scope LUs of the group before selecting (see §6.2). Synthetic rows are generated only for any remaining unfilled slots.

### 6.2 Bus number duplicate check for OOS LUs

Before a candidate OOS LU is selected to fill a slot, its `bus_number` must be checked against all `bus_number` values already present in the in-scope LUs of the same group. If the OOS LU's `bus_number` is a duplicate of an existing in-scope `bus_number`, it must be flagged in feedback but may still be used — the duplicate is recorded so the implementer is aware. The check uses the same normalization logic as §6 outlier duplicate normalization (strip common prefixes, strip leading zeros).

### 6.3 REUSE output rows

OOS LUs selected to fill ADD slots are emitted with:

- `action = REUSE`
- All original field values (`jvid`, `bus_number`, `status`, `delivery_status`, etc.) carried through from the OOS feature
- `MDU_ID`, `BG_ID`, `house_number`, `house_number_extension` from the group
- Feedback: `"Reused OOS LU — scopelist_v15: <value>"` if `scopelist_v15` was populated, or `"Reused OOS LU — no scopelist_v15"` if not
- If the `bus_number` was a duplicate of an in-scope LU: append `"; Warning: duplicate bus_number (X) with in-scope LU"`

### 6.4 Synthetic ADD rows

Generated only when OOS pool for the group is exhausted:

- `action = ADD`
- `jvid`, `status`, `delivery_status` → empty string
- `bus_number` → algorithmically suggested (see §7)
- `MDU_ID`, `BG_ID`, `house_number`, `house_number_extension` inherited from the group
- Feedback: `"New LU — suggested bus_number: X"`
- Suggested `bus_number` values must not duplicate any existing `bus_number` in the group (in-scope or already-assigned REUSE rows)

---

## 7. Bus Number Outlier Detection

Given the list of `bus_number` values in a group, flag values as outliers using the following checks applied in order:

### 7.1 Non-dominant prefix

- Identify the most common letter prefix across all `bus_number` values.
- If the dominant prefix appears more than once, flag any `bus_number` with a different prefix as an outlier.
- Reason: `"bus_number outlier (X)"`

### 7.2 Wrong digit length

- Within the dominant prefix group, determine the majority digit count.
- Flag values whose digit count differs from the majority as outliers.
- **Exception:** skip this check when the numeric sequence naturally crosses a power-of-10 boundary (e.g. a sequence containing both 9 and 10 is normal counting, not an anomaly).

### 7.3 Duplicate normalization

- Strip common prefixes: `bte`, `box`, `apt`, `app`, `flat`, `unit` (case-insensitive).
- Strip leading zeros from the remaining numeric part.
- If two `bus_number` values normalize to the same string, keep the one matching the dominant format; flag the other as an outlier.

---

## 8. Bus Number Sequence Suggestion (Synthetic ADD rows)

Given existing `bus_number` values in a group and `n` missing LUs to fill:

| Step | Description |
|---|---|
| 1 | Exclude outliers (§7) from the analysis. |
| 2 | Identify the dominant prefix from remaining values. |
| 3 | Pad all numbers to the same digit length. |
| 4 | Analyse each digit position: identify **varying** positions (range min ≠ max) vs **fixed** positions. |
| 5 | The **leftmost varying position** is the "floor/wing" dimension — extend its maximum by 1 per new group needed. |
| 6 | **Inner varying positions** define the "unit" dimension — repeat all existing combinations for each new floor/wing value. |
| 7 | Never suggest a value already present in the group's `bus_number` list (in-scope or REUSE). |

**Example:** `B101, B102, B201, B202` → floor = hundreds digit (varies 1–2), unit = units digit (varies 1–2) → suggest `B301, B302, B401, B402`.

---

## 9. Bus Number Sort Key (Logical Order)

When sorting `bus_number` values for candidate selection or removal ordering:

| Priority | Condition | Sort key |
|---|---|---|
| 1 (first) | Parseable (prefix + numeric) | `(0, prefix, numeric_value)` |
| 2 | Non-standard / non-parseable format | After parseable values |
| 3 (last) | Empty / null `bus_number` | Always last |

---

## 10. FME Integration

### 10.1 PythonCaller class structure

| Element | Detail |
|---|---|
| Class name | `FeatureProcessor` |
| Methods | `__init__`, `input(feature)`, `close()`, `process_group()` |
| Input ports | `INPUT` (in-scope LUs), `OOS_INPUT` (out-of-scope LUs) |
| Pattern | Batch mode — collect all features in `input()`; self-group and process in `close()`; emit one `FMEFeature` per LU via `self.pyoutput(out)` |
| FME version | 2024.2 (Build 24783, WIN64) |
| Python version | 3.12 (FME bundled) |
| Dependencies | `fmeobjects`, `re`, `collections` — no external packages |
| Script delivery | Pasted inline into the PythonCaller editor (not referenced as an external file) |

### 10.2 Self-grouping in `close()`

- In `input(feature)`, inspect the feature's source port name and append to `self.in_scope_features` or `self.oos_features` accordingly.
- In `close()`, build two dicts keyed by resolved group key: `in_scope_by_group` and `oos_by_group`.
- For each group key present in `in_scope_by_group`, call `process_group(group_key)`.
- Within `process_group()`, retrieve the matching OOS LUs from `oos_by_group` (empty list if none).

### 10.3 Output attributes to declare in PythonCaller (all varchar)

`MDU_ID`, `BG_ID`, `house_number`, `house_number_extension`, `jvid`, `bus_number`, `status`, `delivery_status`, `action`, `feedback`

---

## 11. Feedback Value Reference

| action | feedback template |
|---|---|
| `KEEP` | `"Kept: no grouping key (BG_ID and MDU_ID both absent)"` |
| `KEEP` | `"Kept: NULL bus_number retained (MDU becoming SDU)"` |
| `KEEP` | `"Kept (candidate but not needed): <reason1>, <reason2>, ..."` |
| `KEEP` | *(no feedback — ordinary keep; duplicate warning preserved if applicable)* |
| `REMOVE` | `"Remove: <reason1>, <reason2>, ..."` |
| `REMOVE` | `"Remove: removed from end (no candidate flags)"` |
| `REUSE` | `"Reused OOS LU — scopelist_v15: <value>"` |
| `REUSE` | `"Reused OOS LU — no scopelist_v15"` |
| `REUSE` | *(append)* `"; Warning: duplicate bus_number (X) with in-scope LU"` |
| `ADD` | `"New LU — suggested bus_number: X"` |
| any | `"Warning: duplicate bus_number (X) in source data"` *(set before action logic; overwritten by more specific feedback)* |
