# LU Reconciliation — Requirements Prompt

## Context

This script reconciles Logement Unit (LU) counts between a Deployment List (DL) and a Build List (BL) for Belgian fiber deployment. It runs as an FME 2024.2 PythonCaller (class `FeatureProcessor`, FME API: `input` / `close` / `pyoutput`).

Each incoming FME feature represents one **MDU** (Multi-Dwelling Unit) — a building with multiple LUs. Fields on the MDU feature contain comma-separated values, one entry per LU.

---

## Input Attributes (one FME feature = one MDU)

| Attribute | Description |
|---|---|
| `BL_Review` | `Excessive` / `Add to DL` / `Review` |
| `DMP BUILDING KEY` | Building identifier |
| `DMP HOUSENUMBER` | House number |
| `APP SSV BU COUNT` | Target business unit count (SSV) |
| `APP SSV LU COUNT` | Target LU count (SSV) |
| `Count_DL` | Current LU count in Deployment List |
| `jvid` | Comma-separated LU address keys |
| `bus_number` | Comma-separated apartment/unit identifiers |
| `status` | Comma-separated statuses per LU |
| `delivery_status` | Comma-separated delivery statuses per LU |

**Target LU count** = `APP SSV BU COUNT` + `APP SSV LU COUNT`

**Note:** `jvid` may be dot-separated floats instead of comma-separated (e.g. `10083231.10083232`) — handle both formats.

---

## Output

One FME feature per LU with these attributes:

| Attribute | Description |
|---|---|
| `DMP BUILDING KEY` | Inherited from MDU |
| `DMP HOUSENUMBER` | Inherited from MDU |
| `jvid` | LU address key (empty for ADD rows) |
| `bus_number` | Unit identifier |
| `status` | LU status |
| `delivery_status` | LU delivery status |
| `action` | `KEEP` / `REMOVE` / `ADD` |
| `feedback` | Human-readable explanation of the action |
| `bl_review` | Carried from source (`Excessive` / `Add to DL` / `Review`) |

---

## Core Logic

### Step 1 — Compute delta

- `n_remove = current_LU_count - target`
- `n_add = target - current_LU_count`
- If `n_remove == 0` and `n_add == 0` → all LUs get `action = KEEP`

---

### Step 2 — REMOVE logic (when `n_remove > 0`)

#### 2a. Lock (never removable)
- Any LU with `status = HOMES_CONNECT` → locked, always KEEP
- If `target == 1` (MDU becoming SDU): the first LU with an empty/null `bus_number` → locked, always KEEP
  - Feedback: `"Kept: NULL bus_number retained (MDU becoming SDU)"`

#### 2b. Flag removal candidates (cumulative — all flags apply)
Only apply status-based flags when **statuses are not all alike** across unlocked LUs:
- `delivery_status` starts with `"2"` → candidate, reason: `"delivery_status starts with 2"`
- `delivery_status` does not end with `"5"` (and is not empty) → candidate, reason: `"delivery_status not ending with 5"`

Always apply:
- `bus_number` is empty/null AND there are multiple unlocked LUs → candidate, reason: `"bus_number is empty/null"`
- `bus_number` is a detected outlier (see outlier detection below) → candidate, reason: `"bus_number outlier (X)"`
- `bus_number` is a duplicate of one already seen in this MDU (second+ occurrence) → candidate, reason: `"duplicate bus_number (X)"`

#### 2c. Remove candidates first, then rest
- Sort candidates by `bus_number` in logical order (by numeric value, prefix-aware), remove from the end first
- If still need to remove more after exhausting candidates, sort remaining unlocked LUs the same way and remove from the end
- Feedback for removed: `"Remove: <reason1>, <reason2>, ..."` or `"Remove: removed from end (no candidate flags)"`
- Feedback for kept candidates: `"Kept (candidate but not needed): <reasons>"`

---

### Step 3 — ADD logic (when `n_add > 0`)

- All existing LUs → `action = KEEP`
- Generate `n_add` new LU rows with `action = ADD`
- New rows inherit `DMP BUILDING KEY`, `DMP HOUSENUMBER`, `bl_review` from the MDU
- `jvid`, `status`, `delivery_status` are empty
- `bus_number` is algorithmically suggested (see bus_number suggestion below)
- Feedback: `"New LU — suggested bus_number: X"`
- Suggested bus_numbers must not duplicate any existing bus_number in the MDU

---

## Bus Number Logic

### Outlier Detection
Given a list of bus_numbers, flag as outliers:
1. **Non-dominant prefix**: find most common letter prefix (e.g. `B` in `B101, B102, B201`); flag values with a different prefix if dominant appears more than once
2. **Wrong digit length**: within the dominant prefix group, flag values whose digit count differs from the majority
3. **Duplicate normalization**: strip common prefixes (`bte`, `box`, `apt`, `app`, `flat`, `unit`), strip leading zeros — if two values normalize to the same string, keep the one matching dominant format, flag the other

### Sequence Suggestion
Given existing bus_numbers and `n` missing, suggest `n` next logical values:
1. Exclude outliers from the analysis
2. Find dominant prefix
3. Pad all numbers to the same digit length
4. Analyze each digit position: identify **varying** positions (range min ≠ max) vs **fixed**
5. The **leftmost varying position** is the "floor/wing" dimension — extend its maximum by 1 per new group
6. **Inner varying positions** define the "unit" dimension — repeat all existing combinations for each new floor/wing value
7. Example: `B101, B102, B201, B202` → positions: floor=hundreds (varies 1-2), unit=units (varies 1-2) → suggest `B301, B302, B401, B402`
8. Never suggest a value already present in the MDU's bus_number list

### Sort Key (logical order)
Sort bus_numbers by: `(0, prefix, numeric_value)` for parseable values; non-standard formats after; empty/null last.

---

## Pre-existing Duplicate Warning

Before remove/add logic, scan for duplicate bus_numbers in the source data. For any LU whose bus_number appears more than once in the same MDU, set feedback to: `"Warning: duplicate bus_number (X) in source data"`. This warning is preserved unless overwritten by a more specific remove/keep/add feedback.

---

## FME Integration

- **Class**: `FeatureProcessor`
- **Methods**: `__init__`, `input(feature)`, `close()`, `process_group()`
- **Pattern**: batch mode — collect all MDU features in `input()`, process everything in `close()`, emit one `FMEFeature` per LU via `self.pyoutput(out)`
- **FME version**: 2024.2 (Build 24783, WIN64)
- **Python**: 3.12 (FME bundled)
- **Dependencies**: only `fmeobjects`, `re`, `collections` — no external packages needed

### Output attributes to declare in PythonCaller (all varchar):
`DMP BUILDING KEY`, `DMP HOUSENUMBER`, `jvid`, `bus_number`, `status`, `delivery_status`, `action`, `feedback`, `bl_review`
