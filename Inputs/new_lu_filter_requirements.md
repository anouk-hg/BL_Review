# Filtering Requirements: Newly Added LUs (Post-Reconciliation)

## Context

After LU reconciliation has determined which LUs should be added (`action = ADD`), each newly added LU must be evaluated to determine whether it can be processed automatically or requires manual review. This filter operates at LU level. Each feature represents one individual LU. The key inputs are the SSV quadrant linked to the building group, the count of BL records in the group, the LU's `DMP_delivery_status`, and the SSV total unit count.

The filter produces two output categories:

- **Can be added** — the new LU can proceed without manual intervention
- **Needs review** — the new LU requires manual assessment before it can be added

---

## Resolved Input Attributes

The filter uses resolved attributes that adapt to whether the LU belongs to a Building Group (BG) or a standalone MDU:

| Resolved attribute | When `BG_ID` has a value | When `BG_ID` is absent |
|---|---|---|
| `target_total` | `BG_Total` | `MDU_Total` |
| `count_bl` | `BL_BG_Total` | `BL_MDU_Total` |

These resolved values are used in all filter conditions below.

**Pre-computation (AttributeManager):**

| Attribute | Expression |
|---|---|
| `target_total` | `BG_Total` if `BG_ID` has a value, else `MDU_Total` |
| `count_bl` | `BL_BG_Total` if `BG_ID` has a value, else `BL_MDU_Total` |
| `tolerance` | `max(1, 0.2 × count_bl)` |
| `gap` | `target_total − count_bl` |
| `d_threshold` | Next multiple of 4 strictly greater than `count_bl` |

---

## Spare Capacity Threshold

For building groups with more than one existing BL record (`count_bl > 1`), a spare capacity tolerance is applied when comparing total units against current BL count. The tolerance is defined as:

```
tolerance = max(1, 0.2 × count_bl)
```

The **gap** is defined as:

```
gap = target_total − count_bl
```

- **Small gap**: `gap ≤ tolerance` → within spare capacity → **Can be added**
- **Large gap**: `gap > tolerance` → exceeds spare capacity → **Needs review**

> **Example:** For a building with `count_bl = 10`, tolerance = max(1, 2) = 2. A request adding 1 or 2 units falls within spare capacity (can be added). A request adding 3 or more units exceeds it (needs review).

---

## Filter Cases

Conditions are evaluated in order. The **first matching case** applies.

---

### New MDU — Needs review

| Condition | Value |
|---|---|
| `DMP_delivery_status` | Missing (attribute absent) |

The LU has no delivery status, indicating it is a brand-new LU not yet present in the BL. Always requires review.

---

### Case 1 — Needs review

| Condition | Value |
|---|---|
| `APP SSV QUADRANT` | A, Ax, C, Cx, or Dx |
| `count_bl` | = 1 |

A single existing BL record for a non-D-type quadrant building. No spare capacity planned for single-LU buildings; requires review.

---

### Case 2a — Can be added

| Condition | Value |
|---|---|
| `APP SSV QUADRANT` | D |
| `count_bl` | = 1 |
| `target_total` | ≤ 4 |

Single BL record, quadrant D, and target total fits within the first multiple-of-4 capacity block (1–4 units). Can be added.

---

### Case 2b — Needs review

| Condition | Value |
|---|---|
| `APP SSV QUADRANT` | D |
| `count_bl` | = 1 |
| `target_total` | > 4 |

Single BL record, quadrant D, but target total exceeds the first capacity block. Requires capacity assessment.

---

### Case 3 — Needs review

| Condition | Value |
|---|---|
| `APP SSV QUADRANT` | M or P |
| `count_bl` | = 1 |

Single BL record for a mixed or public quadrant building. No spare capacity planned; requires review.

---

### Case 4a — Can be added

| Condition | Value |
|---|---|
| `APP SSV QUADRANT` | A, Ax, C, or Cx |
| `count_bl` | > 1 |
| `DMP_delivery_status` (first character) | 7 |
| Gap (`target_total − count_bl`) | ≤ tolerance |

Multiple existing BL records, delivery status in the 7xx range, and the addition falls within spare capacity. Can be added.

---

### Case 4b — Needs review

| Condition | Value |
|---|---|
| `APP SSV QUADRANT` | A, Ax, C, or Cx |
| `count_bl` | > 1 |
| `DMP_delivery_status` (first character) | 7 |
| Gap (`target_total − count_bl`) | > tolerance |

Same as Case 4a but the addition exceeds spare capacity. Requires review.

---

### Case 5 — Can be added

| Condition | Value |
|---|---|
| `APP SSV QUADRANT` | A, Ax, C, or Cx |
| `count_bl` | > 1 |
| `DMP_delivery_status` (first character) | 8 |

Multiple existing BL records and delivery status in the 8xx range. No gap threshold applies for this delivery status; can be added directly.

---

### Case 6a — Can be added

| Condition | Value |
|---|---|
| `APP SSV QUADRANT` | A, Ax, C, or Cx |
| `count_bl` | > 1 |
| `DMP_delivery_status` (first character) | 9 |
| Gap (`target_total − count_bl`) | ≤ tolerance |

Multiple existing BL records, delivery status in the 9xx range, and the addition falls within spare capacity. Can be added.

---

### Case 6b — Needs review

| Condition | Value |
|---|---|
| `APP SSV QUADRANT` | A, Ax, C, or Cx |
| `count_bl` | > 1 |
| `DMP_delivery_status` (first character) | 9 |
| Gap (`target_total − count_bl`) | > tolerance |

Same as Case 6a but the addition exceeds spare capacity. Requires review.

---

### Case 7a — Can be added

| Condition | Value |
|---|---|
| `APP SSV QUADRANT` | D or Dx |
| `count_bl` | > 1 |
| `target_total` | ≤ next multiple-of-4 block above `count_bl` |

Multiple existing BL records for a D-type quadrant building, and target total fits within the next multiple-of-4 capacity block above `count_bl`. Can be added.

> **Threshold:** D-type buildings are provisioned in blocks of 4. The threshold is the next block of 4 that is strictly greater than `count_bl`. For example: `count_bl = 4` → threshold = 8; `count_bl = 5` → threshold = 8; `count_bl = 8` → threshold = 12. If target total falls within that block, the addition is within planned capacity.

---

### Case 7b — Needs review

| Condition | Value |
|---|---|
| `APP SSV QUADRANT` | D or Dx |
| `count_bl` | > 1 |
| `target_total` | > threshold (same formula as Case 7a) |

Same as Case 7a but target total exceeds the next capacity block. Requires review.

---

### No SSV — Needs review

| Condition | Value |
|---|---|
| `APP SSV QUADRANT` | Attribute has no value (null/missing) |

No SSV data is available for this building group. Cannot assess capacity. Requires review.

---

### Unfiltered — Needs review

Any LU that does not match any of the above cases. This should not occur under normal conditions and indicates an unexpected data state. Requires review.

---

## Output Summary

| Case | SSV Quadrant | count_bl | Delivery Status | Additional Condition | Output |
|---|---|---|---|---|---|
| New MDU | any | any | Missing | — | Needs review |
| 1 | A, Ax, C, Cx, Dx | = 1 | any | — | Needs review |
| 2a | D | = 1 | any | target_total ≤ 4 | Can be added |
| 2b | D | = 1 | any | target_total > 4 | Needs review |
| 3 | M, P | = 1 | any | — | Needs review |
| 4a | A, Ax, C, Cx | > 1 | starts with 7 | gap ≤ tolerance | Can be added |
| 4b | A, Ax, C, Cx | > 1 | starts with 7 | gap > tolerance | Needs review |
| 5 | A, Ax, C, Cx | > 1 | starts with 8 | — | Can be added |
| 6a | A, Ax, C, Cx | > 1 | starts with 9 | gap ≤ tolerance | Can be added |
| 6b | A, Ax, C, Cx | > 1 | starts with 9 | gap > tolerance | Needs review |
| 7a | D, Dx | > 1 | any | target_total ≤ d_threshold | Can be added |
| 7b | D, Dx | > 1 | any | target_total > d_threshold | Needs review |
| No SSV | — | any | any | SSV quadrant missing | Needs review |
| Unfiltered | — | — | — | No case matched | Needs review |
