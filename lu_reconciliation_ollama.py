# -*- coding: utf-8 -*-
"""
LU Reconciliation for FME 2024.2 PythonCaller — with optional Ollama feedback.

Spec: lu_reconciliation_requirements.md

Input shapes (BL-DL-SSV_Rework.fmw — Aggregator / Aggregator_2):

  A) BG-level aggregate (Aggregator): one feature per group
     Group-By: BG_ID, Delta, BG_Total, BL_BG_Total
     Concatenated (comma-aligned per LU): delivery_status, bus_number, jvid, status (incl. HOMES_CONNECT lock)
  B) MDU-level aggregate (Aggregator_2): one feature per group
     Group-By: MDU_ID, Delta, BL_MDU_Total, MDU_Total
     Concatenated: same four fields when status is in the Aggregator list

  Target LU count = BG_Total (path A) or MDU_Total (path B).
  BL-side count = BL_BG_Total or BL_MDU_Total (for reporting / Ollama context).
  Per-LU lists are parsed from the three concat strings (split on comma, strip parts).

  C) Legacy: one feature per MDU with APP SSV BU/LU COUNT + Count_DL + jvid/... as in the spec.

- Deterministic core: KEEP / REMOVE / ADD, locks, candidates, bus_number logic.
  Per-LU `status` from Aggregator concat: if `HOMES_CONNECT`, that LU is locked (never REMOVE).

- Ollama (optional): enriches per-LU `feedback` using MDU/BG delta context.

Environment (optional):
  OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_ENABLED, OLLAMA_TIMEOUT — see below.

FME: Class FeatureProcessor, Symbol FeatureProcessor.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from collections import Counter

import fmeobjects

# ---------------------------------------------------------------------------
# Ollama (stdlib HTTP)
# ---------------------------------------------------------------------------

def _ollama_enabled() -> bool:
    return os.environ.get("OLLAMA_ENABLED", "1").strip() not in ("0", "false", "no", "")


def _ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


def _ollama_model() -> str:
    return os.environ.get("OLLAMA_MODEL", "llama3.2").strip()


def _ollama_timeout() -> int:
    try:
        return int(os.environ.get("OLLAMA_TIMEOUT", "120"))
    except ValueError:
        return 120


def ollama_chat(messages: list[dict], *, model: str | None = None) -> str:
    """POST /api/chat; returns assistant message content or raises."""
    host = _ollama_host()
    m = model or _ollama_model()
    payload = {
        "model": m,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.1},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=_ollama_timeout(), context=ctx) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return str(body.get("message", {}).get("content", "")).strip()


def _extract_json_object(text: str) -> dict | None:
    text = text.strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def enrich_feedback_ollama(
    lu_rows: list[dict],
    *,
    building_key: str,
    housenumber: str,
    bl_review: str,
    target: int,
    count_dl: int,
    ssv_text_attrs: dict[str, str | None],
) -> None:
    """
    Mutates lu_rows[*]['feedback'] in place. On any failure, leaves rows unchanged.
    ssv_text_attrs: optional keys Delta, MDU_Total, BG_Total, BL_MDU_Total, BL_BG_Total, BG_ID, MDU_ID
    """
    if not _ollama_enabled() or not lu_rows:
        return
    lines = []
    for i, lu in enumerate(lu_rows):
        lines.append(
            f"{i}|jvid={lu.get('jvid','')!r}|bus={lu.get('bus_number','')!r}|"
            f"action={lu.get('action','')!r}|draft_feedback={lu.get('feedback','')!r}"
        )
    ctx_blob = "\n".join(f"{k}={v!r}" for k, v in sorted(ssv_text_attrs.items()) if v)
    system = (
        "You help Belgian fiber LU reconciliation. Output ONLY valid JSON, no markdown. "
        "Schema: {\"notes\": [\"...\", ...] } with exactly one string per input row index 0..n-1. "
        "Each note is a SHORT (max 25 words) optional add-on explaining how MDU/BG totals relate "
        "to this LU's action. If nothing to add, use empty string. Do not contradict the action."
    )
    user = (
        f"MDU building_key={building_key!r} housenumber={housenumber!r} "
        f"bl_review={bl_review!r} target_LU={target} current_DL_count={count_dl}.\n"
        f"Optional aggregates:\n{ctx_blob or '(none)'}\n"
        f"Rows:\n" + "\n".join(lines)
    )
    try:
        raw = ollama_chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        parsed = _extract_json_object(raw)
        if not parsed or "notes" not in parsed:
            return
        notes = parsed["notes"]
        if not isinstance(notes, list) or len(notes) != len(lu_rows):
            return
        for lu, note in zip(lu_rows, notes):
            n = str(note).strip() if note is not None else ""
            if n:
                base = (lu.get("feedback") or "").strip()
                lu["feedback"] = f"{base} [{n}]" if base else n
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return


# ---------------------------------------------------------------------------
# Spec helpers (lu_reconciliation_requirements.md)
# ---------------------------------------------------------------------------

def _parse_csv(value):
    if value is None or str(value).strip() == "":
        return []
    s = str(value).strip()
    if re.fullmatch(r"[\d.]+", s) and "." in s and "," not in s:
        parts = s.split(".")
        return [p for p in parts if p]
    return [p.strip() for p in s.split(",")]


def _split_agg_csv(val):
    """Split Aggregator concat on commas; keep empty tokens so columns stay aligned."""
    if val is None or str(val).strip() == "":
        return []
    return [p.strip() for p in str(val).split(",")]


def _parse_aggregator_aligned(jvid_attr, bus_attr, delivery_attr, status_attr=None):
    """
    Parse FME Aggregator comma-concatenated fields into aligned lists per LU.
    Includes `status` when present (e.g. HOMES_CONNECT → must not REMOVE per spec).
    """
    jv = _split_agg_csv(jvid_attr)
    bn = _split_agg_csv(bus_attr)
    ds = _split_agg_csv(delivery_attr)
    st = _split_agg_csv(status_attr) if status_attr is not None else []
    n = max(len(jv), len(bn), len(ds), len(st), 1)
    jv.extend([""] * (n - len(jv)))
    bn.extend([""] * (n - len(bn)))
    ds.extend([""] * (n - len(ds)))
    st.extend([""] * (n - len(st)))
    return jv[:n], bn[:n], ds[:n], st[:n]


def _get_attr(feature, name):
    try:
        v = feature.getAttribute(name)
    except Exception:
        v = None
    if v is None:
        return None
    s = str(v).strip()
    return s if s not in ("", "None") else None


def _safe_int(val, default=0):
    try:
        return int(float(str(val)))
    except (ValueError, TypeError):
        return default


def _parse_busnum(bn):
    if not bn:
        return None
    bn = bn.strip()
    m = re.fullmatch(r"([A-Za-z]*)(\d+)", bn)
    if m:
        prefix = m.group(1).upper()
        digits = m.group(2)
        return prefix, digits, int(digits)
    return None


def _sort_key(bn):
    if not bn or not bn.strip():
        return (2, "", 0, "")
    parsed = _parse_busnum(bn.strip())
    if parsed:
        prefix, digits, num = parsed
        return (0, prefix, num, "")
    return (1, "", 0, bn.strip().lower())


def _normalize_busnum(bn):
    if not bn:
        return ""
    s = bn.strip().lower()
    for pfx in ("bte", "box", "apt", "app", "flat", "unit"):
        if s.startswith(pfx):
            s = s[len(pfx) :]
            break
    s = s.lstrip("0") or "0"
    return s


def detect_outliers(bus_numbers):
    outlier_idx = set()
    parsed = [_parse_busnum(bn) if bn and bn.strip() else None for bn in bus_numbers]
    dominant_prefix = ""
    dominant_dl = None
    prefixes = [p[0] for p in parsed if p is not None]
    if prefixes:
        prefix_counts = Counter(prefixes)
        dominant_prefix, dominant_count = prefix_counts.most_common(1)[0]
        if dominant_count > 1:
            for i, p in enumerate(parsed):
                if p is not None and p[0] != dominant_prefix:
                    outlier_idx.add(i)
    dominant_group_idx = [
        i
        for i, p in enumerate(parsed)
        if p is not None and p[0] == dominant_prefix and i not in outlier_idx
    ]
    if dominant_group_idx:
        nums = [parsed[i][2] for i in dominant_group_idx]
        min_num, max_num = min(nums), max(nums)
        range_spans_boundary = len(str(min_num)) != len(str(max_num))
        digit_lengths = [len(parsed[i][1]) for i in dominant_group_idx]
        dl_counts = Counter(digit_lengths)
        dominant_dl, dominant_dl_count = dl_counts.most_common(1)[0]
        total_in_group = len(dominant_group_idx)
        if not range_spans_boundary and dominant_dl_count > 1:
            for i in dominant_group_idx:
                if len(parsed[i][1]) != dominant_dl:
                    outlier_idx.add(i)
    norm_map = {}
    for i, bn in enumerate(bus_numbers):
        if i in outlier_idx or not bn or not bn.strip():
            continue
        norm = _normalize_busnum(bn)
        if norm in norm_map:
            other_i = norm_map[norm]

            def is_dominant(p):
                if p is None:
                    return False
                ok = p[0] == dominant_prefix
                if dominant_group_idx and dominant_dl is not None:
                    ok = ok and (len(p[1]) == dominant_dl)
                return ok

            p_i = parsed[i]
            p_other = parsed[other_i]
            if is_dominant(p_i) and not is_dominant(p_other):
                outlier_idx.add(other_i)
                norm_map[norm] = i
            else:
                outlier_idx.add(i)
        else:
            norm_map[norm] = i
    return outlier_idx


def suggest_bus_numbers(existing, n_add):
    if n_add <= 0:
        return []
    existing_clean = [b for b in existing if b and b.strip()]
    outlier_idx = detect_outliers(existing_clean)
    analysis_pool = [b for i, b in enumerate(existing_clean) if i not in outlier_idx]
    parsed_pool = [_parse_busnum(b) for b in analysis_pool]
    parsed_pool = [p for p in parsed_pool if p is not None]
    existing_set = set(b.strip() for b in existing_clean)
    if not parsed_pool:
        suggestions = []
        counter = 1
        while len(suggestions) < n_add:
            candidate = str(counter)
            if candidate not in existing_set:
                suggestions.append(candidate)
            counter += 1
        return suggestions
    prefixes = [p[0] for p in parsed_pool]
    prefix_counts = Counter(prefixes)
    dominant_prefix = prefix_counts.most_common(1)[0][0]
    dominant_parsed = [p for p in parsed_pool if p[0] == dominant_prefix]
    digit_lengths = [len(p[1]) for p in dominant_parsed]
    dl_counts = Counter(digit_lengths)
    pad_len = dl_counts.most_common(1)[0][0]
    padded_nums = [p[2] for p in dominant_parsed]
    if len(padded_nums) == 1:
        suggestions = []
        next_val = padded_nums[0] + 1
        while len(suggestions) < n_add:
            candidate = dominant_prefix + str(next_val).zfill(pad_len)
            if candidate not in existing_set:
                suggestions.append(candidate)
            next_val += 1
        return suggestions
    padded_strs = [str(v).zfill(pad_len) for v in padded_nums]
    pos_values = []
    for pos in range(pad_len):
        vals = set(s[pos] for s in padded_strs)
        pos_values.append(vals)
    varying_positions = [i for i, vals in enumerate(pos_values) if len(vals) > 1]
    fixed_positions = [i for i, vals in enumerate(pos_values) if len(vals) == 1]
    if not varying_positions:
        suggestions = []
        next_val = max(padded_nums) + 1
        while len(suggestions) < n_add:
            candidate = dominant_prefix + str(next_val).zfill(pad_len)
            if candidate not in existing_set:
                suggestions.append(candidate)
            next_val += 1
        return suggestions
    floor_pos = varying_positions[0]
    inner_positions = varying_positions[1:] if len(varying_positions) > 1 else []
    fixed_digits = {pos: list(pos_values[pos])[0] for pos in fixed_positions}
    floor_vals = sorted(set(int(s[floor_pos]) for s in padded_strs))
    max_floor = max(floor_vals)
    if inner_positions:
        inner_combinations = sorted(
            set(tuple(s[p] for p in inner_positions) for s in padded_strs)
        )
    else:
        inner_combinations = [()]
    suggestions = []
    next_floor = max_floor + 1
    while len(suggestions) < n_add:
        for combo in inner_combinations:
            if len(suggestions) >= n_add:
                break
            chars = ["0"] * pad_len
            for pos, digit in fixed_digits.items():
                chars[pos] = digit
            chars[floor_pos] = str(next_floor)
            for p, d in zip(inner_positions, combo):
                chars[p] = d
            candidate = dominant_prefix + "".join(chars)
            if candidate not in existing_set:
                suggestions.append(candidate)
                existing_set.add(candidate)
        next_floor += 1
        if next_floor > 99:
            break
    if len(suggestions) < n_add:
        max_val = max(padded_nums)
        counter = max_val + 1
        while len(suggestions) < n_add:
            candidate = dominant_prefix + str(counter).zfill(pad_len)
            if candidate not in existing_set:
                suggestions.append(candidate)
                existing_set.add(candidate)
            counter += 1
    return suggestions


def process_mdu(mdu: dict) -> list[dict]:
    """Returns list of LU dicts (spec: action KEEP/REMOVE/ADD, feedback, ...)."""
    building_key = mdu["building_key"]
    housenumber = mdu["housenumber"]
    bl_review = mdu["bl_review"]
    target = mdu["target"]
    jvids = mdu["jvids"]
    bus_numbers = mdu["bus_numbers"]
    statuses = mdu["statuses"]
    delivery_statuses = mdu["delivery_statuses"]
    n_lu = max(len(jvids), len(bus_numbers), len(statuses), len(delivery_statuses), 1)

    def pad(lst, length):
        return lst + [""] * (length - len(lst))

    jvids = pad(jvids, n_lu)
    bus_numbers = pad(bus_numbers, n_lu)
    statuses = pad(statuses, n_lu)
    delivery_statuses = pad(delivery_statuses, n_lu)
    lus = []
    for i in range(n_lu):
        lus.append(
            {
                "jvid": jvids[i],
                "bus_number": bus_numbers[i],
                "status": statuses[i],
                "delivery_status": delivery_statuses[i],
                "action": "KEEP",
                "feedback": "",
                "building_key": building_key,
                "housenumber": housenumber,
                "bl_review": bl_review,
            }
        )
    bn_count = Counter(lu["bus_number"] for lu in lus if lu["bus_number"])
    for lu in lus:
        bn = lu["bus_number"]
        if bn and bn_count[bn] > 1:
            lu["feedback"] = f"Warning: duplicate bus_number ({bn}) in source data"
    n_remove = n_lu - target
    n_add = target - n_lu
    if n_remove == 0 and n_add == 0:
        return lus
    if n_remove > 0:
        locked = [False] * n_lu
        for i, lu in enumerate(lus):
            st = (lu.get("status") or "").strip()
            if st.upper() == "HOMES_CONNECT":
                locked[i] = True
        if target == 1:
            for i, lu in enumerate(lus):
                if not lu["bus_number"]:
                    locked[i] = True
                    lu["feedback"] = "Kept: NULL bus_number retained (MDU becoming SDU)"
                    break
        unlocked_indices = [i for i in range(n_lu) if not locked[i]]
        unlocked_statuses_eq = (
            len(
                set(
                    str(lus[i]["delivery_status"])
                    if lus[i]["delivery_status"] is not None
                    else ""
                    for i in unlocked_indices
                )
            )
            <= 1
        )
        seen_bus_numbers = {}
        candidate_flags = {i: [] for i in unlocked_indices}
        unlocked_bns = [lus[i]["bus_number"] for i in unlocked_indices]
        outlier_idx_set = detect_outliers(unlocked_bns)
        outlier_global = {unlocked_indices[j] for j in outlier_idx_set}
        for i in unlocked_indices:
            lu = lus[i]
            ds = str(lu["delivery_status"]) if lu["delivery_status"] is not None else ""
            bn = lu["bus_number"]
            if not unlocked_statuses_eq:
                if ds and ds.startswith("2"):
                    candidate_flags[i].append("delivery_status starts with 2")
                if ds and not ds.endswith("5"):
                    candidate_flags[i].append("delivery_status not ending with 5")
            if not bn and len(unlocked_indices) > 1:
                candidate_flags[i].append("bus_number is empty/null")
            if i in outlier_global:
                candidate_flags[i].append(f"bus_number outlier ({bn})")
            if bn:
                if bn in seen_bus_numbers:
                    candidate_flags[i].append(f"duplicate bus_number ({bn})")
                else:
                    seen_bus_numbers[bn] = i
        candidates = [i for i in unlocked_indices if candidate_flags[i]]
        non_candidates = [i for i in unlocked_indices if not candidate_flags[i]]
        candidates_sorted = sorted(candidates, key=lambda i: _sort_key(lus[i]["bus_number"]))
        non_candidates_sorted = sorted(
            non_candidates, key=lambda i: _sort_key(lus[i]["bus_number"])
        )
        to_remove = []
        remaining_remove = n_remove
        while remaining_remove > 0 and candidates_sorted:
            idx = candidates_sorted.pop()
            to_remove.append(idx)
            remaining_remove -= 1
        while remaining_remove > 0 and non_candidates_sorted:
            idx = non_candidates_sorted.pop()
            to_remove.append(idx)
            remaining_remove -= 1
        to_remove_set = set(to_remove)
        for i in unlocked_indices:
            lu = lus[i]
            if i in to_remove_set:
                flags = candidate_flags[i]
                if flags:
                    lu["feedback"] = "Remove: " + ", ".join(flags)
                else:
                    lu["feedback"] = "Remove: removed from end (no candidate flags)"
                lu["action"] = "REMOVE"
            else:
                flags = candidate_flags[i]
                if flags:
                    lu["feedback"] = "Kept (candidate but not needed): " + ", ".join(flags)
    if n_add > 0:
        existing_bns = [lu["bus_number"] for lu in lus]
        suggestions = suggest_bus_numbers(existing_bns, n_add)
        for k in range(n_add):
            suggested = suggestions[k] if k < len(suggestions) else ""
            lus.append(
                {
                    "jvid": "",
                    "bus_number": suggested,
                    "status": "",
                    "delivery_status": "",
                    "action": "ADD",
                    "feedback": f"New LU — suggested bus_number: {suggested}",
                    "building_key": building_key,
                    "housenumber": housenumber,
                    "bl_review": bl_review,
                }
            )
    return lus


# ---------------------------------------------------------------------------
# FME FeatureProcessor
# ---------------------------------------------------------------------------

_OPTIONAL_CTX_ATTRS = (
    "Delta",
    "MDU_Total",
    "BG_Total",
    "BL_MDU_Total",
    "BL_BG_Total",
    "BG_ID",
    "MDU_ID",
)


def _building_key_from_feature(ga):
    return ga("DMP BUILDING KEY") or ga("belmap_building_id") or ""


def _housenumber_from_feature(ga):
    h = ga("DMP HOUSENUMBER")
    if h:
        return h
    hn = ga("house_number") or ""
    he = ga("house_number_extension") or ""
    return (hn + (he or "")).strip() or ""


class FeatureProcessor(object):
    def __init__(self):
        self._mdus = []

    def input(self, feature):
        def ga(name):
            return _get_attr(feature, name)

        md_id = ga("MDU_ID")
        bg_id = ga("BG_ID")

        bl_review = ga("BL_Review") or ""

        if md_id:
            # Aggregator_2: GROUP_BY MDU_ID Delta BL_MDU_Total MDU_Total;
            # CONCAT includes delivery_status, bus_number, jvid, status (match FME order)
            target = _safe_int(ga("MDU_Total"), 0)
            count_dl = _safe_int(ga("BL_MDU_Total"), 0)
            jvids, bus_numbers, delivery_statuses, statuses = _parse_aggregator_aligned(
                ga("jvid"),
                ga("bus_number"),
                ga("delivery_status"),
                ga("status"),
            )
            group_scope = "MDU"
        elif bg_id:
            # Aggregator: GROUP_BY BG_ID Delta BG_Total BL_BG_Total;
            target = _safe_int(ga("BG_Total"), 0)
            count_dl = _safe_int(ga("BL_BG_Total"), 0)
            jvids, bus_numbers, delivery_statuses, statuses = _parse_aggregator_aligned(
                ga("jvid"),
                ga("bus_number"),
                ga("delivery_status"),
                ga("status"),
            )
            group_scope = "BG"
        else:
            # Legacy: single MDU feature (Monday / join output)
            group_scope = "LEGACY"
            ssv_bu = _safe_int(ga("APP SSV BU COUNT"), 0)
            ssv_lu = _safe_int(ga("APP SSV LU COUNT"), 0)
            target = ssv_bu + ssv_lu
            count_dl = _safe_int(ga("Count_DL"), 0)
            jvids = _parse_csv(ga("jvid"))
            bus_numbers = _parse_csv(ga("bus_number"))
            statuses = _parse_csv(ga("status"))
            delivery_statuses = _parse_csv(ga("delivery_status"))

        building_key = _building_key_from_feature(ga)
        housenumber = _housenumber_from_feature(ga)

        ctx = {k: ga(k) for k in _OPTIONAL_CTX_ATTRS}
        ctx["group_scope"] = group_scope

        self._mdus.append(
            {
                "building_key": building_key,
                "housenumber": housenumber,
                "bl_review": bl_review,
                "target": target,
                "count_dl": count_dl,
                "jvids": jvids,
                "bus_numbers": bus_numbers,
                "statuses": statuses,
                "delivery_statuses": delivery_statuses,
                "ctx": ctx,
                "group_scope": group_scope,
            }
        )

    def close(self):
        for mdu in self._mdus:
            lu_rows = process_mdu(mdu)
            enrich_feedback_ollama(
                lu_rows,
                building_key=mdu["building_key"] or "",
                housenumber=mdu["housenumber"] or "",
                bl_review=mdu["bl_review"] or "",
                target=mdu["target"],
                count_dl=mdu["count_dl"],
                ssv_text_attrs={k: v for k, v in mdu["ctx"].items() if v},
            )
            for lu in lu_rows:
                out = fmeobjects.FMEFeature()
                out.setAttribute("DMP BUILDING KEY", lu["building_key"] or "")
                out.setAttribute("DMP HOUSENUMBER", lu["housenumber"] or "")
                out.setAttribute("jvid", lu["jvid"] or "")
                out.setAttribute("bus_number", lu["bus_number"] or "")
                out.setAttribute("status", lu["status"] or "")
                dlv = lu["delivery_status"]
                out.setAttribute(
                    "delivery_status", "" if dlv is None else str(dlv)
                )
                # KEEP / REMOVE / ADD — always string; LU_ACTION mirrors action for
                # writers (e.g. XLSX) that sometimes drop a column named "action".
                act = str(lu.get("action") or "KEEP").strip().upper()
                if act not in ("KEEP", "REMOVE", "ADD"):
                    act = "KEEP"
                out.setAttribute("action", act)
                out.setAttribute("LU_ACTION", act)
                out.setAttribute("feedback", lu.get("feedback") or "")
                out.setAttribute("bl_review", lu["bl_review"] or "")
                out.setAttribute("group_scope", mdu.get("group_scope") or "")
                if mdu["ctx"].get("BG_ID"):
                    out.setAttribute("BG_ID", mdu["ctx"]["BG_ID"])
                if mdu["ctx"].get("MDU_ID"):
                    out.setAttribute("MDU_ID", mdu["ctx"]["MDU_ID"])
                if mdu["ctx"].get("Delta") is not None and str(
                    mdu["ctx"].get("Delta", "")
                ).strip() != "":
                    out.setAttribute("Delta", mdu["ctx"]["Delta"])
                self.pyoutput(out)


# ---------------------------------------------------------------------------
# FME PythonCaller — NEW_ATTRIBUTES (required for output file columns)
#
# If `action` is missing in the written file, the transformer almost always
# needs every output attribute declared here (and matching the Writer schema).
# Include both names so at least one survives picky formats:
#
#   action varchar(20)
#   LU_ACTION varchar(20)
#   DMP BUILDING KEY varchar(200)
#   DMP HOUSENUMBER varchar(200)
#   jvid varchar(200)
#   bus_number varchar(200)
#   status varchar(255)
#   delivery_status varchar(200)
#   feedback varchar(2000)
#   bl_review varchar(200)
#   group_scope varchar(20)
#   BG_ID varchar(200)
#   MDU_ID varchar(200)
#   Delta varchar(200)
# ---------------------------------------------------------------------------
