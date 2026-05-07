#!/usr/bin/env python3
"""
FME Workspace Parser (fmw_parser.py)

Parses FME .fmw workspace files and extracts structured metadata,
transformer pipelines, data flow graphs, and human-readable summaries.

No external dependencies -- uses only the Python standard library.

Usage:
    python fmw_parser.py <file.fmw>              # Summary report
    python fmw_parser.py <file.fmw> --detail     # Detailed report
    python fmw_parser.py <file.fmw> --json       # Structured JSON
    python fmw_parser.py <file.fmw> --graph      # Mermaid data flow diagram
    python fmw_parser.py <file.fmw> --all        # All outputs combined
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field, asdict
from html.parser import HTMLParser
from typing import Optional


# =============================================================================
# FME Text Encoding/Decoding
# =============================================================================

FME_TOKEN_MAP = {
    "space": " ",
    "lt": "<",
    "gt": ">",
    "apos": "'",
    "quote": '"',
    "openparen": "(",
    "closeparen": ")",
    "solidus": "/",
    "at": "@",
    "comma": ",",
    "opencurly": "{",
    "closecurly": "}",
    "backslash": "\\",
    "lf": "\n",
    "cr": "\r",
    "openbracket": "[",
    "closebracket": "]",
    "amp": "&",
    "semicolon": ";",
    "colon": ":",
    "hash": "#",
    "dollar": "$",
    "percent": "%",
    "caret": "^",
    "tilde": "~",
    "pipe": "|",
    "plus": "+",
    "equals": "=",
    "excl": "!",
    "question": "?",
    "hyphen": "-",
    "underscore": "_",
    "period": ".",
    "tab": "\t",
}

_FME_TOKEN_RE = re.compile(r"<(\w+)>")


def decode_fme_text(text: str) -> str:
    """Decode FME encoded text tokens like <space>, <lt>, <apos>, etc."""
    if not text or "<" not in text:
        return text

    def _replace(m):
        token = m.group(1).lower()
        if token in FME_TOKEN_MAP:
            return FME_TOKEN_MAP[token]
        # Handle Unicode tokens like <u2014>
        if token.startswith("u") and len(token) == 5:
            try:
                return chr(int(token[1:], 16))
            except ValueError:
                pass
        return m.group(0)  # return original if unknown

    return _FME_TOKEN_RE.sub(_replace, text)


# =============================================================================
# HTML Text Extraction
# =============================================================================

class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []
        self._skip = False  # skip content inside <style>, <head> tags

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "head", "script"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("style", "head", "script"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def get_text(self):
        text = " ".join(self._parts).strip()
        # Collapse whitespace
        text = re.sub(r'\s+', ' ', text)
        return text


def extract_text_from_html(html_str: str) -> str:
    """Extract plain text from HTML-encoded comment values."""
    if not html_str:
        return ""
    # Unescape XML/HTML numeric character references
    html_str = html_str.replace("&#10;", "\n").replace("&#13;", "\r")
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html_str)
        return parser.get_text()
    except Exception:
        return html_str


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class WorkspaceMetadata:
    fme_build: str = ""
    last_save_build: str = ""
    last_save_date: str = ""
    guid: str = ""
    title: str = ""
    description: str = ""
    python_version: str = ""
    geometry_handling: str = ""


@dataclass
class Dataset:
    is_source: bool = True
    role: str = ""
    format: str = ""
    dataset: str = ""
    keyword: str = ""
    enabled: bool = True
    dataset_type: str = ""


@dataclass
class FeatureAttribute:
    name: str = ""
    attr_type: str = ""
    has_port: bool = False
    value: str = ""


@dataclass
class FeatureType:
    is_source: bool = True
    node_name: str = ""
    qualifier: str = ""
    identifier: str = ""
    keyword: str = ""
    position: str = ""
    enabled: bool = True
    attributes: list[FeatureAttribute] = field(default_factory=list)
    where_clause: str = ""


@dataclass
class TransformerParam:
    name: str = ""
    value: str = ""
    is_lookup: bool = False


@dataclass
class TransformerAttribute:
    name: str = ""
    is_user_created: bool = False
    feat_index: int = 0


@dataclass
class OutputPort:
    name: str = ""
    feat_index: int = 0
    collapsed: bool = False


@dataclass
class Transformer:
    identifier: str = ""
    type: str = ""
    version: str = ""
    position: str = ""
    enabled: bool = True
    xformer_name: str = ""
    output_ports: list[OutputPort] = field(default_factory=list)
    parameters: dict[str, TransformerParam] = field(default_factory=dict)
    attributes: list[TransformerAttribute] = field(default_factory=list)


@dataclass
class FeatLink:
    identifier: str = ""
    source_node: str = ""
    target_node: str = ""
    source_port_desc: str = ""
    target_port_desc: str = ""
    enabled: bool = True


@dataclass
class Bookmark:
    identifier: str = ""
    name: str = ""
    description: str = ""
    contents: list[str] = field(default_factory=list)
    color: str = ""
    position: str = ""


@dataclass
class GlobalParameter:
    name: str = ""
    gui_line: str = ""
    default_value: str = ""
    is_stand_alone: bool = True


@dataclass
class UserParameter:
    name: str = ""
    default_value: str = ""
    scope: str = ""
    gui_line: str = ""


@dataclass
class Comment:
    identifier: str = ""
    text: str = ""
    position: str = ""
    anchored_node: str = ""


@dataclass
class WriterDataset:
    name: str = ""
    dataset: str = ""
    override: str = ""


@dataclass
class Connection:
    name: str = ""
    type: str = ""
    family: str = ""
    usage: str = ""


@dataclass
class FactoryDef:
    factory_type: str = ""
    factory_name: str = ""
    raw_line: str = ""


@dataclass
class FMWWorkspace:
    filename: str = ""
    metadata: WorkspaceMetadata = field(default_factory=WorkspaceMetadata)
    datasets: list[Dataset] = field(default_factory=list)
    feature_types: list[FeatureType] = field(default_factory=list)
    transformers: list[Transformer] = field(default_factory=list)
    feat_links: list[FeatLink] = field(default_factory=list)
    bookmarks: list[Bookmark] = field(default_factory=list)
    global_parameters: list[GlobalParameter] = field(default_factory=list)
    user_parameters: list[UserParameter] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    writer_datasets: list[WriterDataset] = field(default_factory=list)
    connections: list[Connection] = field(default_factory=list)
    factory_defs: list[FactoryDef] = field(default_factory=list)


# =============================================================================
# XML Section Parser
# =============================================================================

def _attr(elem, name, default=""):
    """Get an XML attribute value, decoded from FME encoding."""
    val = elem.get(name, default)
    return decode_fme_text(val) if val else default


def _bool_attr(elem, name, default=True):
    val = elem.get(name, "")
    if val.lower() in ("true", "yes"):
        return True
    if val.lower() in ("false", "no"):
        return False
    return default


class FMWXmlParser:
    """Parses the XML metadata section of an .fmw file."""

    def __init__(self, xml_content: str):
        self.root = ET.fromstring(xml_content)

    def parse_metadata(self) -> WorkspaceMetadata:
        r = self.root
        return WorkspaceMetadata(
            fme_build=r.get("FME_BUILD_NUM", ""),
            last_save_build=r.get("LAST_SAVE_BUILD", ""),
            last_save_date=r.get("LAST_SAVE_DATE", ""),
            guid=r.get("FME_DOCUMENT_GUID", ""),
            title=r.get("TITLE", ""),
            description=r.get("DESCRIPTION", ""),
            python_version=r.get("PYTHON_COMPATIBILITY", ""),
            geometry_handling=r.get("FME_GEOMETRY_HANDLING", ""),
        )

    def parse_datasets(self) -> list[Dataset]:
        results = []
        for elem in self.root.findall(".//DATASETS/DATASET"):
            results.append(Dataset(
                is_source=_bool_attr(elem, "IS_SOURCE", True),
                role=elem.get("ROLE", ""),
                format=elem.get("FORMAT", ""),
                dataset=decode_fme_text(elem.get("DATASET", "")),
                keyword=elem.get("KEYWORD", ""),
                enabled=_bool_attr(elem, "ENABLED", True),
                dataset_type=elem.get("DATASET_TYPE", ""),
            ))
        return results

    def parse_feature_types(self) -> list[FeatureType]:
        results = []
        for elem in self.root.findall(".//FEATURE_TYPES/FEATURE_TYPE"):
            attrs = []
            for fa in elem.findall("FEAT_ATTRIBUTE"):
                attrs.append(FeatureAttribute(
                    name=decode_fme_text(fa.get("ATTR_NAME", "")),
                    attr_type=fa.get("ATTR_TYPE", ""),
                    has_port=_bool_attr(fa, "ATTR_HAS_PORT", False),
                    value=fa.get("ATTR_VALUE", ""),
                ))

            where_clause = ""
            for dp in elem.findall("DEFLINE_PARM"):
                if dp.get("PARM_NAME") == "postgis_sql_where_clause":
                    where_clause = decode_fme_text(dp.get("PARM_VALUE", ""))

            results.append(FeatureType(
                is_source=_bool_attr(elem, "IS_SOURCE", True),
                node_name=elem.get("NODE_NAME", ""),
                qualifier=elem.get("FEATURE_TYPE_NAME_QUALIFIER", ""),
                identifier=elem.get("IDENTIFIER", ""),
                keyword=elem.get("KEYWORD", ""),
                position=elem.get("POSITION", ""),
                enabled=_bool_attr(elem, "ENABLED", True),
                attributes=attrs,
                where_clause=where_clause,
            ))
        return results

    def parse_transformers(self) -> list[Transformer]:
        results = []
        for elem in self.root.findall(".//TRANSFORMERS/TRANSFORMER"):
            output_ports = []
            xf_attrs = []
            params = {}

            port_idx = 0
            for child in elem:
                tag = child.tag
                if tag == "OUTPUT_FEAT":
                    output_ports.append(OutputPort(
                        name=decode_fme_text(child.get("NAME", "")),
                        feat_index=port_idx,
                    ))
                    port_idx += 1
                elif tag == "FEAT_COLLAPSED":
                    collapsed = child.get("COLLAPSED", "0") != "0"
                    if output_ports:
                        output_ports[-1].collapsed = collapsed
                elif tag == "XFORM_ATTR":
                    xf_attrs.append(TransformerAttribute(
                        name=decode_fme_text(child.get("ATTR_NAME", "")),
                        is_user_created=_bool_attr(child, "IS_USER_CREATED", False),
                        feat_index=int(child.get("FEAT_INDEX", "0")),
                    ))
                elif tag == "XFORM_PARM":
                    pname = child.get("PARM_NAME", "")
                    params[pname] = TransformerParam(
                        name=pname,
                        value=decode_fme_text(child.get("PARM_VALUE", "")),
                        is_lookup=_bool_attr(child, "PARM_IS_LOOKUP", False),
                    )

            xformer_name = ""
            if "XFORMER_NAME" in params:
                xformer_name = params["XFORMER_NAME"].value

            results.append(Transformer(
                identifier=elem.get("IDENTIFIER", ""),
                type=elem.get("TYPE", ""),
                version=elem.get("VERSION", ""),
                position=elem.get("POSITION", ""),
                enabled=_bool_attr(elem, "ENABLED", True),
                xformer_name=xformer_name,
                output_ports=output_ports,
                parameters=params,
                attributes=xf_attrs,
            ))
        return results

    def parse_feat_links(self) -> list[FeatLink]:
        results = []
        for elem in self.root.findall(".//FEAT_LINKS/FEAT_LINK"):
            results.append(FeatLink(
                identifier=elem.get("IDENTIFIER", ""),
                source_node=elem.get("SOURCE_NODE", ""),
                target_node=elem.get("TARGET_NODE", ""),
                source_port_desc=decode_fme_text(elem.get("SOURCE_PORT_DESC", "")),
                target_port_desc=decode_fme_text(elem.get("TARGET_PORT_DESC", "")),
                enabled=_bool_attr(elem, "ENABLED", True),
            ))
        return results

    def parse_bookmarks(self) -> list[Bookmark]:
        results = []
        for elem in self.root.findall(".//BOOKMARKS/BOOKMARK"):
            # CONTENTS is a space-separated string of node IDs
            contents_str = elem.get("CONTENTS", "")
            contents = [c for c in contents_str.split() if c]

            results.append(Bookmark(
                identifier=elem.get("IDENTIFIER", ""),
                name=decode_fme_text(elem.get("NAME", "")),
                description=decode_fme_text(elem.get("DESCRIPTION", "")),
                contents=contents,
                color=elem.get("COLOUR", ""),
                position=elem.get("TOP_LEFT", ""),
            ))
        return results

    def parse_global_parameters(self) -> list[GlobalParameter]:
        results = []
        for elem in self.root.findall(".//GLOBAL_PARAMETERS/GLOBAL_PARAMETER"):
            gui_line = elem.get("GUI_LINE", "")
            name = ""
            # Extract parameter name from GUI_LINE
            parts = gui_line.split()
            if len(parts) >= 3:
                name = parts[2] if parts[0] == "GUI" else ""

            results.append(GlobalParameter(
                name=name,
                gui_line=gui_line,
                default_value=decode_fme_text(elem.get("DEFAULT_VALUE", "")),
                is_stand_alone=_bool_attr(elem, "IS_STAND_ALONE", True),
            ))
        return results

    def parse_user_parameters(self) -> list[UserParameter]:
        results = []
        up_elem = self.root.find(".//USER_PARAMETERS")
        if up_elem is not None:
            # Try to decode the base64 FORM attribute for richer info
            form_b64 = up_elem.get("FORM", "")
            form_data = None
            if form_b64:
                try:
                    form_data = json.loads(base64.b64decode(form_b64))
                except Exception:
                    pass

            for info in up_elem.findall(".//PARAMETER_INFO/INFO"):
                results.append(UserParameter(
                    name=info.get("NAME", ""),
                    default_value=decode_fme_text(info.get("DEFAULT_VALUE", "")),
                    scope=info.get("SCOPE", ""),
                    gui_line=info.get("GUI_LINE", ""),
                ))
        return results

    def parse_comments(self) -> list[Comment]:
        results = []
        for elem in self.root.findall(".//COMMENTS/COMMENT"):
            raw_html = elem.get("COMMENT_VALUE", "")
            text = extract_text_from_html(raw_html)
            results.append(Comment(
                identifier=elem.get("IDENTIFIER", ""),
                text=text,
                position=elem.get("POSITION", ""),
                anchored_node=elem.get("ANCHORED_NODE", ""),
            ))
        return results

    def parse_writer_datasets(self) -> list[WriterDataset]:
        results = []
        for elem in self.root.findall(".//FMESERVER/WRITER_DATASETS/DATASET"):
            results.append(WriterDataset(
                name=elem.get("NAME", ""),
                dataset=decode_fme_text(elem.get("DATASET", "")),
                override=elem.get("OVERRIDE", ""),
            ))
        return results

    def parse_connections(self) -> list[Connection]:
        results = []
        for elem in self.root.findall(".//FMESERVER/CONNECTIONS/CONNECTION"):
            results.append(Connection(
                name=decode_fme_text(elem.get("NAME", "")),
                type=elem.get("TYPE", ""),
                family=elem.get("FAMILY", ""),
                usage=elem.get("USAGE", ""),
            ))
        return results


# =============================================================================
# Mapping File Parser
# =============================================================================

_FACTORY_NAME_RE = re.compile(r'FACTORY_NAME\s+(?:"([^"]+)"|\{\s*([^}]+?)\s*\})')
_FACTORY_TYPE_RE = re.compile(r'FACTORY_DEF\s+\{?\*\}?\s+(\w+)')


class FMWMappingParser:
    """Parses the mapping file section (TCL-based directives)."""

    def __init__(self, lines: list[str]):
        self.lines = lines

    def parse_factory_defs(self) -> list[FactoryDef]:
        results = []
        for line in self.lines:
            stripped = line.strip()
            if not stripped.startswith("FACTORY_DEF"):
                continue

            decoded = decode_fme_text(stripped)

            ftype_m = _FACTORY_TYPE_RE.search(decoded)
            fname_m = _FACTORY_NAME_RE.search(decoded)

            results.append(FactoryDef(
                factory_type=ftype_m.group(1) if ftype_m else "",
                factory_name=(fname_m.group(1) or fname_m.group(2) or "").strip() if fname_m else "",
                raw_line=decoded,
            ))
        return results


# =============================================================================
# Data Flow Graph
# =============================================================================

class DataFlowGraph:
    """Directed graph of the workspace data flow."""

    def __init__(self, workspace: FMWWorkspace):
        self.workspace = workspace
        self._node_map: dict[str, FeatureType | Transformer] = {}
        self._adjacency: dict[str, list[tuple[str, str]]] = {}  # src -> [(tgt, port)]
        self._reverse: dict[str, list[str]] = {}  # tgt -> [src]
        self._build()

    def _build(self):
        for ft in self.workspace.feature_types:
            self._node_map[ft.identifier] = ft
        for t in self.workspace.transformers:
            self._node_map[t.identifier] = t

        for link in self.workspace.feat_links:
            if not link.enabled:
                continue
            src = link.source_node
            tgt = link.target_node
            port = link.target_port_desc
            self._adjacency.setdefault(src, []).append((tgt, port))
            self._reverse.setdefault(tgt, []).append(src)

    def get_node_label(self, node_id: str) -> str:
        node = self._node_map.get(node_id)
        if node is None:
            return f"Unknown({node_id})"
        if isinstance(node, FeatureType):
            q = f"{node.qualifier}." if node.qualifier else ""
            return f"{q}{node.node_name}"
        if isinstance(node, Transformer):
            label = node.xformer_name or node.type
            return f"{node.type}: {label}" if node.xformer_name and node.xformer_name != node.type else node.type
        return str(node_id)

    def get_sources(self) -> list[str]:
        """Nodes with no incoming edges (source feature types)."""
        return [nid for nid in self._node_map
                if nid not in self._reverse and nid in self._adjacency]

    def get_sinks(self) -> list[str]:
        """Nodes with no outgoing edges (writers, dead ends)."""
        return [nid for nid in self._node_map
                if nid not in self._adjacency and nid in self._reverse]

    def get_successors(self, node_id: str) -> list[str]:
        return [tgt for tgt, _ in self._adjacency.get(node_id, [])]

    def get_predecessors(self, node_id: str) -> list[str]:
        return self._reverse.get(node_id, [])

    def _sanitize_mermaid_id(self, node_id: str) -> str:
        return f"N{node_id}"

    def _sanitize_mermaid_label(self, label: str) -> str:
        # Escape characters problematic for Mermaid
        return label.replace('"', "'").replace("[", "(").replace("]", ")")

    def to_mermaid(self) -> str:
        """Generate a Mermaid flowchart of the data flow."""
        lines = ["graph LR"]

        # Define node shapes based on type
        for nid, node in self._node_map.items():
            mid = self._sanitize_mermaid_id(nid)
            label = self._sanitize_mermaid_label(self.get_node_label(nid))

            if isinstance(node, FeatureType):
                if node.is_source:
                    lines.append(f"    {mid}[/\"{label}\"/]")  # parallelogram for readers
                else:
                    lines.append(f"    {mid}[[\"{label}\"]]")  # subroutine for writers
            elif isinstance(node, Transformer):
                ttype = node.type
                if "Filter" in ttype or "Test" in ttype:
                    lines.append(f"    {mid}{{\"{label}\"}}")  # diamond for filters
                elif "Writer" in ttype:
                    lines.append(f"    {mid}[[\"{label}\"]]")  # subroutine for writers
                elif "Joiner" in ttype or "Merger" in ttype:
                    lines.append(f"    {mid}([\"{label}\"])")  # stadium for joiners
                else:
                    lines.append(f"    {mid}[\"{label}\"]")  # rectangle default
            else:
                lines.append(f"    {mid}[\"{label}\"]")

        # Define edges
        seen_edges = set()
        for link in self.workspace.feat_links:
            if not link.enabled:
                continue
            src_mid = self._sanitize_mermaid_id(link.source_node)
            tgt_mid = self._sanitize_mermaid_id(link.target_node)
            edge_key = (src_mid, tgt_mid)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)

            # Only add edge if both nodes exist
            if link.source_node in self._node_map and link.target_node in self._node_map:
                lines.append(f"    {src_mid} --> {tgt_mid}")

        # Add bookmarks as subgraphs
        for bm in self.workspace.bookmarks:
            if not bm.contents:
                continue
            bm_label = self._sanitize_mermaid_label(bm.name)
            lines.append(f"    subgraph \"{bm_label}\"")
            for cid in bm.contents:
                if cid in self._node_map:
                    lines.append(f"        {self._sanitize_mermaid_id(cid)}")
            lines.append("    end")

        return "\n".join(lines)


# =============================================================================
# Report Generator
# =============================================================================

class FMWReporter:
    """Generates human-readable reports from a parsed workspace."""

    def __init__(self, workspace: FMWWorkspace):
        self.ws = workspace
        self.graph = DataFlowGraph(workspace)

    def summary(self) -> str:
        ws = self.ws
        lines = []
        lines.append("=" * 70)
        lines.append(f"FME Workspace: {ws.filename}")
        lines.append("=" * 70)
        lines.append("")

        # Metadata
        m = ws.metadata
        if m.last_save_build:
            lines.append(f"FME Build:    {m.last_save_build}")
        if m.last_save_date:
            lines.append(f"Last Saved:   {m.last_save_date}")
        if m.guid:
            lines.append(f"GUID:         {m.guid}")
        if m.python_version:
            lines.append(f"Python:       {m.python_version}")
        lines.append("")

        # Sources
        lines.append("--- Sources (Readers) ---")
        source_datasets = [d for d in ws.datasets if d.is_source]
        if source_datasets:
            for ds in source_datasets:
                lines.append(f"  Format:     {ds.format}")
                lines.append(f"  Connection: {ds.dataset}")
                lines.append(f"  Keyword:    {ds.keyword}")
        source_fts = [ft for ft in ws.feature_types if ft.is_source]
        if source_fts:
            lines.append("  Tables:")
            for ft in source_fts:
                q = f"{ft.qualifier}." if ft.qualifier else ""
                attr_count = len(ft.attributes)
                lines.append(f"    - {q}{ft.node_name} ({attr_count} attributes)")
                if ft.where_clause:
                    lines.append(f"      WHERE: {ft.where_clause}")
        lines.append("")

        # Destinations
        lines.append("--- Destinations (Writers) ---")
        if ws.writer_datasets:
            for wd in ws.writer_datasets:
                lines.append(f"  - {wd.name}: {wd.dataset}")
        else:
            lines.append("  (none defined)")
        lines.append("")

        # Database Connections
        if ws.connections:
            lines.append("--- Database Connections ---")
            for conn in ws.connections:
                lines.append(f"  - {conn.name} ({conn.family}, {conn.type})")
            lines.append("")

        # Published Parameters
        lines.append("--- Published Parameters ---")
        if ws.user_parameters:
            for up in ws.user_parameters:
                lines.append(f"  - {up.name} = \"{up.default_value}\" [{up.scope}]")
        elif ws.global_parameters:
            for gp in ws.global_parameters:
                lines.append(f"  - {gp.name} = \"{gp.default_value}\"")
        lines.append("")

        # Transformers summary
        type_counts = Counter(t.type for t in ws.transformers)
        total = len(ws.transformers)
        lines.append(f"--- Transformers ({total} total) ---")
        for ttype, count in type_counts.most_common():
            lines.append(f"  {ttype}: {count}")
        lines.append("")

        # Bookmarks
        if ws.bookmarks:
            lines.append("--- Bookmarks (Processing Stages) ---")
            for bm in ws.bookmarks:
                n_nodes = len(bm.contents)
                lines.append(f"  - {bm.name} ({n_nodes} nodes)")
            lines.append("")

        # Comments
        non_empty_comments = [c for c in ws.comments if c.text.strip()]
        if non_empty_comments:
            lines.append("--- Annotations ---")
            for c in non_empty_comments:
                text_preview = c.text[:100].replace("\n", " ")
                if len(c.text) > 100:
                    text_preview += "..."
                anchor = f" [on node {c.anchored_node}]" if c.anchored_node else ""
                lines.append(f"  - {text_preview}{anchor}")
            lines.append("")

        # Data flow summary
        sources = self.graph.get_sources()
        sinks = self.graph.get_sinks()
        lines.append("--- Data Flow ---")
        lines.append(f"  Source nodes: {len(sources)}")
        for sid in sources:
            lines.append(f"    - {self.graph.get_node_label(sid)}")
        lines.append(f"  Sink nodes:   {len(sinks)}")
        for sid in sinks:
            lines.append(f"    - {self.graph.get_node_label(sid)}")
        lines.append(f"  Connections:  {len(ws.feat_links)}")
        lines.append("")

        return "\n".join(lines)

    def detail(self) -> str:
        ws = self.ws
        lines = []

        # Start with summary
        lines.append(self.summary())
        lines.append("=" * 70)
        lines.append("DETAILED TRANSFORMER REPORT")
        lines.append("=" * 70)
        lines.append("")

        for t in ws.transformers:
            lines.append(f"--- [{t.identifier}] {t.type}: {t.xformer_name or t.type} ---")
            lines.append(f"  Enabled:  {t.enabled}")
            lines.append(f"  Position: {t.position}")

            # Output ports
            if t.output_ports:
                lines.append("  Output Ports:")
                for port in t.output_ports:
                    lines.append(f"    - {port.name}")

            # Key parameters
            key_params = ["ATTR_TABLE", "TEST_LIST", "JOIN_KEYS", "GROUP_BY",
                          "DATASET", "FORMAT", "FEATURE_TYPE", "PYTHONSOURCE",
                          "JOIN_MODE", "MODE", "AGGREGATE_TYPE", "COUNT_ATTR",
                          "CONCAT_ATTRS", "SEP"]
            shown_params = []
            for kp in key_params:
                if kp in t.parameters and t.parameters[kp].value and t.parameters[kp].value != "<Unused>":
                    shown_params.append((kp, t.parameters[kp].value))

            if shown_params:
                lines.append("  Key Parameters:")
                for pname, pval in shown_params:
                    # Truncate very long values
                    display = pval[:200] + "..." if len(pval) > 200 else pval
                    lines.append(f"    {pname}: {display}")

            # Connections
            preds = self.graph.get_predecessors(t.identifier)
            succs = self.graph.get_successors(t.identifier)
            if preds:
                lines.append("  Inputs from:")
                for pid in preds:
                    lines.append(f"    <- {self.graph.get_node_label(pid)}")
            if succs:
                lines.append("  Outputs to:")
                for sid in succs:
                    lines.append(f"    -> {self.graph.get_node_label(sid)}")

            # Attributes (first port only, summarized)
            port0_attrs = [a for a in t.attributes if a.feat_index == 0]
            if port0_attrs:
                lines.append(f"  Attributes ({len(port0_attrs)}):")
                for a in port0_attrs[:10]:
                    lines.append(f"    - {a.name}")
                if len(port0_attrs) > 10:
                    lines.append(f"    ... and {len(port0_attrs) - 10} more")

            lines.append("")

        return "\n".join(lines)

    def to_json(self) -> str:
        """Serialize the workspace to JSON."""

        def _serialize(obj):
            if hasattr(obj, "__dataclass_fields__"):
                d = {}
                for fname in obj.__dataclass_fields__:
                    val = getattr(obj, fname)
                    d[fname] = _serialize(val)
                return d
            elif isinstance(obj, dict):
                return {k: _serialize(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [_serialize(v) for v in obj]
            else:
                return obj

        data = _serialize(self.ws)
        return json.dumps(data, indent=2, ensure_ascii=False)

    def mermaid(self) -> str:
        return self.graph.to_mermaid()


# =============================================================================
# Main Parser Facade
# =============================================================================

class FMWParser:
    """Main entry point for parsing .fmw files."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.filename = os.path.basename(filepath)

    def parse(self) -> FMWWorkspace:
        with open(self.filepath, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()

        xml_parts = []
        mapping_lines = []
        in_mapping = False

        for line in raw_lines:
            stripped = line.rstrip("\n").rstrip("\r")
            if not in_mapping:
                if stripped.startswith("#!"):
                    xml_content = stripped[2:]  # strip '#!'
                    # Some lines have '#! ' with a leading space
                    xml_parts.append(xml_content)
                    # Check if we just closed the WORKSPACE
                    if "</WORKSPACE>" in xml_content:
                        in_mapping = True
                # Skip plain comments and blank lines within XML section
            else:
                # Mapping file section
                if not stripped.startswith("#"):
                    mapping_lines.append(stripped)

        # Join XML and parse
        # Remove XML declaration if present (ElementTree handles it automatically
        # but it may have leading whitespace after stripping #!)
        xml_str = "\n".join(xml_parts)
        xml_str = xml_str.strip()
        # Remove XML processing instruction if present
        xml_str = re.sub(r'^<\?xml[^?]*\?>\s*', '', xml_str, count=1)

        xml_parser = FMWXmlParser(xml_str)
        mapping_parser = FMWMappingParser(mapping_lines)

        workspace = FMWWorkspace(
            filename=self.filename,
            metadata=xml_parser.parse_metadata(),
            datasets=xml_parser.parse_datasets(),
            feature_types=xml_parser.parse_feature_types(),
            transformers=xml_parser.parse_transformers(),
            feat_links=xml_parser.parse_feat_links(),
            bookmarks=xml_parser.parse_bookmarks(),
            global_parameters=xml_parser.parse_global_parameters(),
            user_parameters=xml_parser.parse_user_parameters(),
            comments=xml_parser.parse_comments(),
            writer_datasets=xml_parser.parse_writer_datasets(),
            connections=xml_parser.parse_connections(),
            factory_defs=mapping_parser.parse_factory_defs(),
        )

        return workspace


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Parse FME .fmw workspace files and extract structured metadata.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("file", help="Path to the .fmw file")
    parser.add_argument("--detail", action="store_true", help="Detailed transformer report")
    parser.add_argument("--json", action="store_true", dest="output_json", help="Output structured JSON")
    parser.add_argument("--graph", action="store_true", help="Output Mermaid data flow diagram")
    parser.add_argument("--all", action="store_true", help="Output everything (summary + detail + graph)")
    parser.add_argument("--output", "-o", help="Write output to file instead of stdout")

    args = parser.parse_args()

    if not os.path.isfile(args.file):
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    fmw = FMWParser(args.file)
    workspace = fmw.parse()
    reporter = FMWReporter(workspace)

    output_parts = []

    if args.output_json:
        output_parts.append(reporter.to_json())
    elif args.all:
        output_parts.append(reporter.detail())
        output_parts.append("\n" + "=" * 70)
        output_parts.append("MERMAID DATA FLOW DIAGRAM")
        output_parts.append("=" * 70 + "\n")
        output_parts.append("```mermaid")
        output_parts.append(reporter.mermaid())
        output_parts.append("```")
    elif args.graph:
        output_parts.append("```mermaid")
        output_parts.append(reporter.mermaid())
        output_parts.append("```")
    elif args.detail:
        output_parts.append(reporter.detail())
    else:
        output_parts.append(reporter.summary())

    result = "\n".join(output_parts)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"Output written to: {args.output}")
    else:
        # Handle Windows console encoding issues
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
        try:
            print(result)
        except UnicodeEncodeError:
            print(result.encode("utf-8", errors="replace").decode("utf-8"))


if __name__ == "__main__":
    main()
