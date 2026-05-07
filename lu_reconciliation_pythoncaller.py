# -*- coding: utf-8 -*-
"""
LU Reconciliation PythonCaller — v2 (LU-Level Input Edition).

Spec: lu_reconciliation_requirements_v2.md

Two named input ports:
  INPUT     — in-scope LU rows (primary reconciliation dataset)
  OOS_INPUT — out-of-scope LU rows (pool for reuse before synthetic ADD)

Self-groups features by BG_ID (priority) or MDU_ID.
Target LU count = BG_Total (when BG_ID present) or MDU_Total.
Current LU count = number of in-scope features in the group.

Actions: KEEP, REMOVE, REUSE, ADD.
ADD slots filled in priority order:
  1. OOS LUs with scopelist_v15 populated
  2. OOS LUs without scopelist_v15
  3. Synthetic ADD rows (bus_number suggested algorithmically)

Ollama integration (optional, env OLLAMA_ENABLED):
  After deterministic logic, Ollama reviews edge cases:
  - REMOVE: validates which LUs to remove when candidates are ambiguous
  - ADD: validates/improves suggested bus_numbers for irregular patterns

FME: Class FeatureProcessor, Symbol FeatureProcessor.
     FME 2024.2, Python 3.12.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from collections import Counter, defaultdict

import fmeobjects


# ───────────────────────────────────────────────────────────────────────────
# Attribute helpers
# ───────────────────────────────────────────────────────────────────────────

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

def _group_key(lu):
    """Resolve grouping key: BG_ID (priority) or MDU_ID. None if ungroupable."""
    bg = lu.get("BG_ID")
    if bg:
        return ("BG", bg)
    md = lu.get("MDU_ID")
    if md:
        return ("MDU", md)
    return None


# ───────────────────────────────────────────────────────────────────────────
# Ollama helpers (optional — edge-case advisor for REMOVE / ADD decisions)
#
# Env vars: OLLAMA_ENABLED (default "1"), OLLAMA_HOST, OLLAMA_MODEL,
#           OLLAMA_TIMEOUT.
# Fails silently — deterministic logic always runs first.
# ───────────────────────────────────────────────────────────────────────────

def _ollama_enabled():
    return os.environ.get(
        "OLLAMA_ENABLED", "1").strip() not in ("0", "false", "no", "")

def _ollama_chat(messages, *, model=None):
    host = os.environ.get(
        "OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    m = model or os.environ.get("OLLAMA_MODEL", "llama3.2").strip()
    try:
        timeout = int(os.environ.get("OLLAMA_TIMEOUT", "120"))
    except ValueError:
        timeout = 120
    data = json.dumps({
        "model": m, "messages": messages,
        "stream": False, "options": {"temperature": 0.1},
    }).encode()
    req = urllib.request.Request(
        f"{host}/api/chat", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        body = json.loads(resp.read().decode())
    return str(body.get("message", {}).get("content", "")).strip()

def _ollama_json(raw):
    """Extract first JSON object from LLM response."""
    m = re.search(r"\{[\s\S]*\}", (raw or "").strip())
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def ollama_review_removals(in_lus, to_remove_indices, group_id):
    """
    Ask Ollama to review REMOVE decisions for edge cases.

    Sends the full LU list with current action assignments.
    Returns a dict {index: note} to append to feedback, or empty.
    """
    if not _ollama_enabled() or not to_remove_indices:
        return {}
    rows = []
    for i, lu in enumerate(in_lus):
        rows.append(
            f"{i}|bus={lu.get('bus_number','')!r}"
            f"|delivery_status={lu.get('delivery_status','')!r}"
            f"|status={lu.get('status','')!r}"
            f"|action={lu.get('action','')}")
    system = (
        "You are a Belgian fiber deployment assistant reviewing LU "
        "removal decisions for an MDU/BG group. "
        "Output ONLY valid JSON, no markdown. "
        'Schema: {"notes": {"<index>": "<note>", ...}} '
        "Only include entries where you see a concern or useful context. "
        "Each note max 30 words. Do not contradict locked/HOMES_CONNECT "
        "decisions."
    )
    user = (
        f"Group={group_id!r}. "
        f"Indices marked REMOVE: {sorted(to_remove_indices)}.\n"
        f"LU rows:\n" + "\n".join(rows)
    )
    try:
        raw = _ollama_chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user}])
        parsed = _ollama_json(raw)
        if not parsed or "notes" not in parsed:
            return {}
        notes = parsed["notes"]
        if not isinstance(notes, dict):
            return {}
        return {int(k): str(v).strip()
                for k, v in notes.items()
                if str(v).strip()}
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, OSError, json.JSONDecodeError,
            ValueError, KeyError):
        return {}


def ollama_review_additions(existing_bns, suggestions, group_id):
    """
    Ask Ollama to validate/improve bus_number suggestions for ADD rows.

    Returns a list of replacement suggestions (same length), or None
    if Ollama can't improve.
    """
    if not _ollama_enabled() or not suggestions:
        return None
    system = (
        "You are a Belgian fiber deployment assistant. "
        "Review bus_number suggestions for new LUs being added to an MDU/BG. "
        "Output ONLY valid JSON, no markdown. "
        'Schema: {"suggestions": ["val1", ...], "changed": true/false} '
        "Return changed=false if the original suggestions are fine. "
        "If you improve them, return the full list with changed=true. "
        "New values must NOT duplicate any existing bus_number. "
        "Follow the dominant prefix+digit pattern of existing values."
    )
    user = (
        f"Group={group_id!r}.\n"
        f"Existing bus_numbers: {existing_bns!r}\n"
        f"Algorithm suggestions: {suggestions!r}\n"
        "Are these suggestions correct for this building's pattern?"
    )
    try:
        raw = _ollama_chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user}])
        parsed = _ollama_json(raw)
        if not parsed:
            return None
        if not parsed.get("changed", False):
            return None
        new_sugg = parsed.get("suggestions")
        if (not isinstance(new_sugg, list)
                or len(new_sugg) != len(suggestions)):
            return None
        # Validate: no duplicates with existing
        used = set(b.strip() for b in existing_bns if b and b.strip())
        for s in new_sugg:
            if not isinstance(s, str) or not s.strip():
                return None
            if s.strip() in used:
                return None
        return [s.strip() for s in new_sugg]
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, OSError, json.JSONDecodeError,
            ValueError, KeyError):
        return None


# ───────────────────────────────────────────────────────────────────────────
# Bus-number logic (§7, §8, §9)
# ───────────────────────────────────────────────────────────────────────────

def _parse_busnum(bn):
    if not bn:
        return None
    m = re.fullmatch(r"([A-Za-z]*)(\d+)", bn.strip())
    if m:
        return m.group(1).upper(), m.group(2), int(m.group(2))
    return None

def _sort_key(bn):
    """§9 — logical sort order for bus_numbers."""
    if not bn or not bn.strip():
        return (2, "", 0, "")
    p = _parse_busnum(bn.strip())
    if p:
        return (0, p[0], p[2], "")
    return (1, "", 0, bn.strip().lower())

def _normalize_busnum(bn):
    """§7.3 — normalize for duplicate detection."""
    if not bn:
        return ""
    s = bn.strip().lower()
    for pfx in ("bte", "box", "apt", "app", "flat", "unit"):
        if s.startswith(pfx):
            s = s[len(pfx):]
            break
    return s.lstrip("0") or "0"

def detect_outliers(bus_numbers):
    """§7 — return set of outlier indices."""
    outlier_idx = set()
    parsed = [_parse_busnum(bn) if bn and bn.strip() else None
              for bn in bus_numbers]
    prefixes = [p[0] for p in parsed if p is not None]
    if not prefixes:
        return outlier_idx

    prefix_counts = Counter(prefixes)
    dominant_prefix, dom_count = prefix_counts.most_common(1)[0]

    # §7.1 Non-dominant prefix
    if dom_count > 1:
        for i, p in enumerate(parsed):
            if p is not None and p[0] != dominant_prefix:
                outlier_idx.add(i)

    # §7.2 Wrong digit length (skip if range spans power-of-10 boundary)
    dom_group = [i for i, p in enumerate(parsed)
                 if p is not None and p[0] == dominant_prefix
                 and i not in outlier_idx]
    dominant_dl = None
    if dom_group:
        nums = [parsed[i][2] for i in dom_group]
        range_spans = len(str(min(nums))) != len(str(max(nums)))
        dl_counts = Counter(len(parsed[i][1]) for i in dom_group)
        dominant_dl = dl_counts.most_common(1)[0][0]
        if not range_spans and dl_counts.most_common(1)[0][1] > 1:
            for i in dom_group:
                if len(parsed[i][1]) != dominant_dl:
                    outlier_idx.add(i)

    # §7.3 Duplicate normalisation
    norm_map = {}
    for i, bn in enumerate(bus_numbers):
        if i in outlier_idx or not bn or not bn.strip():
            continue
        norm = _normalize_busnum(bn)
        if norm in norm_map:
            other = norm_map[norm]
            def _is_dom(p):
                if p is None:
                    return False
                ok = p[0] == dominant_prefix
                if dominant_dl is not None:
                    ok = ok and len(p[1]) == dominant_dl
                return ok
            if _is_dom(parsed[i]) and not _is_dom(parsed[other]):
                outlier_idx.add(other)
                norm_map[norm] = i
            else:
                outlier_idx.add(i)
        else:
            norm_map[norm] = i
    return outlier_idx

def suggest_bus_numbers(existing, n_add):
    """§8 — suggest n_add new bus_numbers following the dominant pattern."""
    if n_add <= 0:
        return []
    clean = [b for b in existing if b and b.strip()]
    outliers = detect_outliers(clean)
    pool = [b for i, b in enumerate(clean) if i not in outliers]
    parsed_pool = [p for p in (_parse_busnum(b) for b in pool)
                   if p is not None]
    used = set(b.strip() for b in clean)

    if not parsed_pool:
        sugg, c = [], 1
        while len(sugg) < n_add:
            if str(c) not in used:
                sugg.append(str(c))
            c += 1
        return sugg

    prefix_counts = Counter(p[0] for p in parsed_pool)
    dom_prefix = prefix_counts.most_common(1)[0][0]
    dom_parsed = [p for p in parsed_pool if p[0] == dom_prefix]
    dl_counts = Counter(len(p[1]) for p in dom_parsed)
    pad_len = dl_counts.most_common(1)[0][0]
    nums = [p[2] for p in dom_parsed]

    if len(nums) == 1:
        sugg, nv = [], nums[0] + 1
        while len(sugg) < n_add:
            c = dom_prefix + str(nv).zfill(pad_len)
            if c not in used:
                sugg.append(c)
            nv += 1
        return sugg

    padded = [str(v).zfill(pad_len) for v in nums]
    pos_vals = [set(s[pos] for s in padded) for pos in range(pad_len)]
    varying = [i for i, vs in enumerate(pos_vals) if len(vs) > 1]
    fixed = {i: list(vs)[0] for i, vs in enumerate(pos_vals)
             if len(vs) == 1}

    if not varying:
        sugg, nv = [], max(nums) + 1
        while len(sugg) < n_add:
            c = dom_prefix + str(nv).zfill(pad_len)
            if c not in used:
                sugg.append(c)
            nv += 1
        return sugg

    floor_pos = varying[0]
    inner = varying[1:]
    max_floor = max(int(s[floor_pos]) for s in padded)
    inner_combos = sorted(set(
        tuple(s[p] for p in inner) for s in padded
    )) if inner else [()]

    sugg = []
    nf = max_floor + 1
    while len(sugg) < n_add and nf <= 99:
        for combo in inner_combos:
            if len(sugg) >= n_add:
                break
            chars = ["0"] * pad_len
            for p, d in fixed.items():
                chars[p] = d
            chars[floor_pos] = str(nf)
            for p, d in zip(inner, combo):
                chars[p] = d
            c = dom_prefix + "".join(chars)
            if c not in used:
                sugg.append(c)
                used.add(c)
        nf += 1

    if len(sugg) < n_add:
        nv = max(nums) + 1
        while len(sugg) < n_add:
            c = dom_prefix + str(nv).zfill(pad_len)
            if c not in used:
                sugg.append(c)
                used.add(c)
            nv += 1
    return sugg


# ───────────────────────────────────────────────────────────────────────────
# Core reconciliation (§4, §5, §6)
# ───────────────────────────────────────────────────────────────────────────

def process_group(in_lus, oos_lus, group_key):
    """
    §4–§6. Reconcile one group.

    in_lus:   list of dicts — in-scope LUs (from INPUT port).
    oos_lus:  list of dicts — out-of-scope LUs (from OOS_INPUT port).
    Returns:  list of dicts — all output LUs (KEEP/REMOVE/REUSE/ADD).
    """
    scope, gid = group_key
    first = in_lus[0]
    if scope == "BG":
        target = _safe_int(first.get("BG_Total"), 0)
    else:
        target = _safe_int(first.get("MDU_Total"), 0)

    # §4.2 current count = in-scope LUs only
    n_lu = len(in_lus)

    # Initialise action/feedback on every in-scope LU
    for lu in in_lus:
        lu["action"] = "KEEP"
        lu["feedback"] = ""

    # §4.3 Pre-existing duplicate bus_number warning (scoped per address)
    addr_bn_count = Counter(
        (lu.get("house_number") or "", lu.get("house_number_extension") or "",
         lu.get("bus_number") or "")
        for lu in in_lus if lu.get("bus_number"))
    for lu in in_lus:
        bn = lu.get("bus_number") or ""
        addr_key = (lu.get("house_number") or "",
                    lu.get("house_number_extension") or "", bn)
        if bn and addr_bn_count[addr_key] > 1:
            lu["feedback"] = (
                f"Warning: duplicate bus_number ({bn}) in source data")

    n_remove = max(n_lu - target, 0)
    n_add = max(target - n_lu, 0)

    if n_remove == 0 and n_add == 0:
        return list(in_lus)

    # ── §5 REMOVE logic ──
    if n_remove > 0:
        locked = [False] * n_lu

        # §5.1 Lock HOMES_CONNECT
        for i, lu in enumerate(in_lus):
            if (lu.get("status") or "").strip().upper() == "HOMES_CONNECT":
                locked[i] = True

        # §5.1 Lock NULL bus_number when MDU becoming SDU (target=1)
        if target == 1:
            for i, lu in enumerate(in_lus):
                if not lu.get("bus_number"):
                    locked[i] = True
                    lu["feedback"] = (
                        "Kept: NULL bus_number retained (MDU becoming SDU)")
                    break

        unlocked = [i for i in range(n_lu) if not locked[i]]

        # §5.2 Uniform delivery_status check
        unlocked_ds = set(
            str(in_lus[i].get("delivery_status") or "")
            for i in unlocked)
        ds_uniform = len(unlocked_ds) <= 1

        # §5.2 Candidate flagging (scoped per address for outliers & duplicates)
        def _addr_key(lu):
            return (lu.get("house_number") or "",
                    lu.get("house_number_extension") or "")

        # Group unlocked indices by address for per-address outlier detection
        addr_groups = defaultdict(list)
        for i in unlocked:
            addr_groups[_addr_key(in_lus[i])].append(i)

        outlier_set = set()
        for addr, indices in addr_groups.items():
            addr_bns = [in_lus[i].get("bus_number") or "" for i in indices]
            for j in detect_outliers(addr_bns):
                outlier_set.add(indices[j])

        # Track seen bus_numbers per address
        seen_bn = {}  # (addr_key, bn) -> index
        cand_flags = {i: [] for i in unlocked}

        for i in unlocked:
            ds = str(in_lus[i].get("delivery_status") or "")
            bn = in_lus[i].get("bus_number") or ""
            addr = _addr_key(in_lus[i])

            if not ds_uniform:
                if ds and ds.startswith("2"):
                    cand_flags[i].append(
                        "delivery_status starts with 2")
                if ds and not ds.endswith("5"):
                    cand_flags[i].append(
                        "delivery_status not ending with 5")

            if not bn and len(unlocked) > 1:
                cand_flags[i].append("bus_number is empty/null")
            if i in outlier_set:
                cand_flags[i].append(f"bus_number outlier ({bn})")
            if bn:
                dup_key = (addr, bn)
                if dup_key in seen_bn:
                    cand_flags[i].append(
                        f"duplicate bus_number ({bn})")
                else:
                    seen_bn[dup_key] = i

        # §5.3 Remove candidates first, then rest (from end)
        candidates = sorted(
            [i for i in unlocked if cand_flags[i]],
            key=lambda i: _sort_key(
                in_lus[i].get("bus_number") or ""))
        non_candidates = sorted(
            [i for i in unlocked if not cand_flags[i]],
            key=lambda i: _sort_key(
                in_lus[i].get("bus_number") or ""))

        to_remove = set()
        remaining = n_remove
        while remaining > 0 and candidates:
            to_remove.add(candidates.pop())
            remaining -= 1
        while remaining > 0 and non_candidates:
            to_remove.add(non_candidates.pop())
            remaining -= 1

        for i in unlocked:
            if i in to_remove:
                flags = cand_flags[i]
                in_lus[i]["action"] = "REMOVE"
                in_lus[i]["feedback"] = (
                    ("Remove: " + ", ".join(flags)) if flags
                    else "Remove: removed from end (no candidate flags)")
            elif cand_flags[i]:
                in_lus[i]["feedback"] = (
                    "Kept (candidate but not needed): "
                    + ", ".join(cand_flags[i]))

        # Ollama: review removal edge cases
        notes = ollama_review_removals(in_lus, to_remove, gid)
        for idx, note in notes.items():
            if 0 <= idx < n_lu:
                base = (in_lus[idx].get("feedback") or "").strip()
                in_lus[idx]["feedback"] = (
                    f"{base} [LLM: {note}]" if base else f"[LLM: {note}]")

        return list(in_lus)

    # ── §6 ADD logic ──
    results = list(in_lus)

    if n_add > 0:
        # Collect in-scope bus_numbers for duplicate checking
        in_scope_norms = {}
        for lu in in_lus:
            bn = lu.get("bus_number") or ""
            if bn:
                in_scope_norms[_normalize_busnum(bn)] = bn

        # Group-level fields for REUSE/ADD inheritance (§6.3, §6.4)
        group_fields = {
            "MDU_ID": first.get("MDU_ID") or "",
            "BG_ID": first.get("BG_ID") or "",
            "house_number": first.get("house_number") or "",
            "house_number_extension": (
                first.get("house_number_extension") or ""),
        }

        filled = 0

        # §6.1 Priority 1: OOS with scopelist_v15 populated
        oos_priority = [lu for lu in oos_lus
                        if lu.get("scopelist_v15")]
        # §6.1 Priority 2: OOS without scopelist_v15
        oos_normal = [lu for lu in oos_lus
                      if not lu.get("scopelist_v15")]

        for oos_pool in [oos_priority, oos_normal]:
            for oos_lu in oos_pool:
                if filled >= n_add:
                    break

                # §6.2 Bus number duplicate check
                oos_bn = oos_lu.get("bus_number") or ""
                is_dup = False
                if oos_bn:
                    norm = _normalize_busnum(oos_bn)
                    if norm in in_scope_norms:
                        is_dup = True

                # §6.3 Build REUSE row — carry all original fields
                reuse_lu = dict(oos_lu)
                reuse_lu["action"] = "REUSE"

                sv15 = oos_lu.get("scopelist_v15")
                if sv15:
                    reuse_lu["feedback"] = (
                        f"Reused OOS LU \u2014 scopelist_v15: {sv15}")
                else:
                    reuse_lu["feedback"] = (
                        "Reused OOS LU \u2014 no scopelist_v15")

                if is_dup:
                    reuse_lu["feedback"] += (
                        f"; Warning: duplicate bus_number ({oos_bn})"
                        " with in-scope LU")

                # Override group identifiers from in-scope
                for k, v in group_fields.items():
                    reuse_lu[k] = v

                results.append(reuse_lu)

                # Track this bus_number as used
                if oos_bn:
                    in_scope_norms[_normalize_busnum(oos_bn)] = oos_bn

                filled += 1

        # §6.4 Synthetic ADD rows for remaining unfilled slots
        remaining_add = n_add - filled
        if remaining_add > 0:
            # Include all bus_numbers so far (in-scope + REUSE)
            all_bns = [lu.get("bus_number") or "" for lu in results
                       if lu.get("bus_number")]
            suggestions = suggest_bus_numbers(all_bns, remaining_add)

            # Ollama: review/improve suggestions for irregular patterns
            improved = ollama_review_additions(all_bns, suggestions, gid)
            if improved:
                suggestions = improved

            for k in range(remaining_add):
                suggested = (suggestions[k]
                             if k < len(suggestions) else "")
                fb = f"New LU \u2014 suggested bus_number: {suggested}"
                if improved:
                    fb += " [LLM-reviewed]"
                add_lu = {
                    "jvid": "",
                    "bus_number": suggested,
                    "status": "",
                    "delivery_status": "",
                    "action": "ADD",
                    "feedback": fb,
                }
                # §6.4 Inherit group fields
                for gk, gv in group_fields.items():
                    add_lu[gk] = gv

                results.append(add_lu)

    return results


# ───────────────────────────────────────────────────────────────────────────
# FME FeatureProcessor (§10)
# ───────────────────────────────────────────────────────────────────────────

# §10.3 Output attributes
_OUTPUT_ATTRS = [
    "MDU_ID", "BG_ID", "house_number", "house_number_extension",
    "jvid", "bus_number", "status", "delivery_status",
    "action", "feedback",
]


class FeatureProcessor(object):
    """§10.1 — batch mode with two input ports."""

    def __init__(self):
        self._in_scope = []
        self._oos = []

    def input(self, feature):
        """§10.2 — route by input port name."""
        # FME sets feature type to the input port name
        port = (feature.getFeatureType() or "").strip()

        lu = {}
        for attr in _OUTPUT_ATTRS:
            lu[attr] = _get_attr(feature, attr)
        # Additional attrs needed for logic but not in output list
        lu["scopelist_v15"] = _get_attr(feature, "scopelist_v15")
        lu["MDU_Total"] = _get_attr(feature, "MDU_Total")
        lu["BG_Total"] = _get_attr(feature, "BG_Total")
        lu["Delta"] = _get_attr(feature, "Delta")

        if port == "OOS_INPUT":
            self._oos.append(lu)
        else:
            self._in_scope.append(lu)

    def close(self):
        """§10.2 — self-group and process."""
        in_scope_by_group = defaultdict(list)
        oos_by_group = defaultdict(list)
        ungroupable = []

        for lu in self._in_scope:
            key = _group_key(lu)
            if key is None:
                ungroupable.append(lu)
            else:
                in_scope_by_group[key].append(lu)

        for lu in self._oos:
            key = _group_key(lu)
            if key is not None:
                oos_by_group[key].append(lu)

        # §2.3 Ungroupable LUs — pass through as KEEP
        for lu in ungroupable:
            lu["action"] = "KEEP"
            lu["feedback"] = (
                "Kept: no grouping key "
                "(BG_ID and MDU_ID both absent)")
            self._emit(lu)

        # Process each group
        for gkey in in_scope_by_group:
            in_lus = in_scope_by_group[gkey]
            oos_lus = oos_by_group.get(gkey, [])
            results = process_group(in_lus, oos_lus, gkey)
            for lu in results:
                self._emit(lu)

    def _emit(self, lu):
        out = fmeobjects.FMEFeature()
        for attr in _OUTPUT_ATTRS:
            val = lu.get(attr)
            out.setAttribute(
                attr, str(val) if val is not None else "")
        self.pyoutput(out)
