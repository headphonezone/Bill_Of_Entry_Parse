"""
BOE (Bill of Entry) PDF Parser
Parses ICEGATE-format Indian Customs Bill of Entry PDFs into a flat,
single-row structure (one row per BOE, with item-level fields repeated
as Item1_*, Item2_*, ... columns).
"""
import re
import pdfplumber

TABLE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
}

NUM_LABEL_RE = re.compile(r"^\d{1,2}[A-Za-z]?\.\s*[A-Za-z]")
LABEL_EXTRACT_RE = re.compile(r"(\d{1,2}[A-Za-z]?\.\s*[A-Za-z][A-Za-z0-9&/,._\-()' ]*)")
MULTILINE_LABEL_HINTS = ("ADDRESS",)
STOP_MARKERS = {"AD CODE", "GLOSSARY", "6. AD CODE"}
MAX_LOOKAHEAD = 8


def clean_cell(text):
    """Clean a table cell: drop leading single-character noise lines
    (artifacts from vertical sidebar text bleeding into the cell),
    then join remaining lines with a space."""
    if text is None:
        return ""
    lines = list(text.split("\n"))
    while len(lines) > 1 and len(lines[0].strip()) <= 1:
        lines.pop(0)
    return " ".join(l.strip() for l in lines if l.strip()).strip()


def strip_label(cell_text):
    """Extract the 'N.Label Text' portion from a cell and return the
    label without its leading number, e.g. '6.CVD' with stray chars
    -> 'CVD'. Returns None if no numbered label found."""
    if not cell_text:
        return None
    m = LABEL_EXTRACT_RE.search(cell_text.replace("\n", " "))
    if not m:
        return None
    raw = m.group(1)
    raw = re.sub(r"^\d{1,2}[A-Za-z]?\.\s*", "", raw).strip()
    return raw


def is_header_row(row):
    """A row is a 'header' (label) row if 2+ cells match a numbered label
    pattern like '1.BCD', '10.T. VALUE', etc."""
    count = 0
    for c in row:
        if strip_label(c):
            count += 1
    return count >= 2


def get_rows(page):
    table = page.extract_table(TABLE_SETTINGS)
    return table or []


def extract_simple_fields(rows):
    """General label:value extractor for non-duty table sections.
    Handles two layouts:
      (1) inline pairs in the same row: label at col i, value at col i+1
      (2) stacked: label at col i in row R, value at SAME col i in a
          later row (until the next multi-label header row)
    Multi-line labels (containing 'ADDRESS') accumulate several lines;
    others take the first value found.
    """
    fields = {}
    n = len(rows)
    for ri, row in enumerate(rows):
        label_positions = [
            (idx, strip_label(c)) for idx, c in enumerate(row) if strip_label(c)
        ]
        if not label_positions:
            continue
        for k, (idx, label) in enumerate(label_positions):
            if not label or fields.get(label):
                continue
            end = label_positions[k + 1][0] if k + 1 < len(label_positions) else len(row)
            # (1) inline search within this row
            value_parts = []
            for j in range(idx + 1, end):
                if j < len(row):
                    v = clean_cell(row[j])
                    if v:
                        value_parts.append(v)
            if value_parts:
                fields[label] = " ".join(value_parts)
                continue
            # (2) stacked search: same column, subsequent rows
            multiline = any(h in label.upper() for h in MULTILINE_LABEL_HINTS)
            collected = []
            window_end = min(end, idx + 3)
            for rj in range(ri + 1, min(ri + 1 + MAX_LOOKAHEAD, n)):
                nrow = rows[rj]
                if is_header_row(nrow):
                    break
                v = ""
                for j in range(idx, min(window_end, len(nrow))):
                    cand = clean_cell(nrow[j])
                    if cand:
                        v = cand
                        break
                if not v:
                    continue
                if multiline and len(v) <= 1:
                    continue
                if v.upper() in STOP_MARKERS:
                    break
                collected.append(v)
                if not multiline:
                    break
            if collected:
                # Keep line breaks for multiline fields (e.g. "Shipper Name
                # and Address") so the first line (name) can be split from
                # the rest (address) later - joining with a plain space
                # would make that unrecoverable.
                fields[label] = "\n".join(collected) if multiline else collected[0]
            else:
                fields.setdefault(label, "")
    return fields


def _find_field(d, *keywords):
    """Find a dict value whose key contains all given keywords (case-insensitive)."""
    for k, v in d.items():
        ku = k.upper()
        if all(kw in ku for kw in keywords):
            return v
    return ""


def shipper_name(header):
    """Extract just the shipper's name - the first line of the combined
    'Shipper Name and Address' field."""
    raw = _find_field(header, "SHIPPER")
    if not raw:
        return ""
    return raw.split("\n")[0].strip()


def parse_label_value_blocks(rows):
    """Group rows into (header_row, [value_rows]) blocks, splitting at
    each row containing 2+ numbered labels. Used for Part III duty
    pages where each item has several stacked duty sub-tables."""
    blocks = []
    current_header = None
    current_values = []
    for row in rows:
        if is_header_row(row):
            if current_header is not None:
                blocks.append((current_header, current_values))
            current_header = row
            current_values = []
        else:
            if current_header is not None and any((c or "").strip() for c in row):
                current_values.append(row)
    if current_header is not None:
        blocks.append((current_header, current_values))
    return blocks


def block_to_duty_fields(header_row, value_rows):
    """For duty blocks: header row has a 'DUTY' marker column plus
    numbered duty-type labels (BCD, IGST, etc). Value rows are tagged
    by a row-name cell (Notn No., Notn SNo., Rate, Amount, Duty Fg) in
    the same column position as the 'DUTY' marker."""
    label_cols = {}
    duty_col = None
    for idx, cell in enumerate(header_row):
        c = (cell or "").strip()
        if c == "DUTY":
            duty_col = idx
        label = strip_label(cell)
        if label:
            label_cols[idx] = label
    fields = {}
    for row in value_rows:
        row_name = None
        if duty_col is not None and duty_col < len(row):
            row_name = clean_cell(row[duty_col])
        if not row_name:
            continue
        for idx, label in label_cols.items():
            if idx < len(row):
                val = clean_cell(row[idx])
                fields[f"{label}__{row_name}"] = val
    return fields


def block_to_simple_fields(header_row, value_rows):
    """For item-info blocks (non-duty): header labels align column-wise
    with a single following value row."""
    fields = {}
    label_cols = {}
    for idx, cell in enumerate(header_row):
        label = strip_label(cell)
        if label:
            label_cols[idx] = label
    merged = {}
    for row in value_rows:
        for idx, cell in enumerate(row):
            val = clean_cell(cell)
            if val and idx not in merged:
                merged[idx] = val
    for idx, label in label_cols.items():
        fields[label] = merged.get(idx, "")
    return fields


TOP_BOX_RE = re.compile(
    r"Port Code.*?\n"
    r"(?P<port>\S+)\s+(?P<beno>\d{5,10})\s+(?P<bedate>\d{2}/\d{2}/\d{4})\s+(?P<betype>\S+)\s*\n"
    r"IEC/Br\s+(?P<iec>\S+)\s*(?P<copy>.*)\n"
    r"GSTIN/TYPE\s+(?P<gstin>\S+)\s*\n"
    r".*?CB CODE\s+(?P<cbcode>\S+)\s*\n"
    r"TYPE\s+INV\s+ITEM\s+CONT\s*\n"
    r".*?Nos\s+(?P<inv>\d+)\s+(?P<item>\d+)\s+(?P<cont>\d+)\s*\n"
    r".*?PKG\s+(?P<pkg>\S+)\s+G\.WT\s*\(KGS\)\s+(?P<gwt>\S+)",
    re.IGNORECASE,
)


def parse_top_box(page):
    """Extract the small always-present header box (Port Code, BE No,
    BE Date, BE Type, IEC/Br, GSTIN/TYPE, CB Code, INV/ITEM/CONT counts,
    PKG, G.WT). These use plain (non-numbered) labels, so they aren't
    picked up by extract_simple_fields; parsed here from plain text
    instead of the table grid, since the layout is simple and stable."""
    txt = page.extract_text() or ""
    m = TOP_BOX_RE.search(txt)
    if not m:
        return {}
    d = m.groupdict()
    return {
        "Port Code": d.get("port", ""),
        "BE No": d.get("beno", ""),
        "BE Date": d.get("bedate", ""),
        "BE Type": d.get("betype", ""),
        "IEC/Br": d.get("iec", ""),
        "GSTIN/TYPE": d.get("gstin", ""),
        "CB CODE": d.get("cbcode", ""),
        "INV": d.get("inv", ""),
        "ITEM": d.get("item", ""),
        "CONT": d.get("cont", ""),
        "PKG": d.get("pkg", ""),
        "G.WT (KGS)": d.get("gwt", ""),
    }


def _page_part_label(page):
    txt = page.extract_text() or ""
    m = re.search(r"PART\s*-\s*([IVX]+)\s*-\s*", txt)
    return m.group(1) if m else None


def _identify_sections(pdf):
    """Return dict part_roman -> list of 0-based page indices."""
    sections = {}
    for i, page in enumerate(pdf.pages):
        part = _page_part_label(page)
        if part:
            sections.setdefault(part, []).append(i)
    return sections


def parse_item_table(rows):
    """Parse the Part II item table rows: [SNo, CTH, Description,
    UnitPrice, Qty, UQC, Amount]. Identifies item rows as those whose
    2nd cell is an 8-digit CTH code."""
    items = {}
    for row in rows:
        cells = [clean_cell(c) for c in row]
        non_empty = [c for c in cells if c]
        if len(non_empty) < 6:
            continue
        sno = non_empty[0]
        cth = non_empty[1] if len(non_empty) > 1 else ""
        if not re.match(r"^\d+$", sno) or not re.match(r"^\d{6,8}$", cth):
            continue
        # last 4 non-empty cells are UnitPrice, Qty, UQC, Amount (in order)
        # description is everything between cth and those last 4
        tail = non_empty[-4:]
        desc_parts = non_empty[2:-4]
        items[sno] = {
            "SNo": sno,
            "CTH": cth,
            "Description": " ".join(desc_parts),
            "UnitPrice": tail[0] if len(tail) > 0 else "",
            "Quantity": tail[1] if len(tail) > 1 else "",
            "UQC": tail[2] if len(tail) > 2 else "",
            "Amount": tail[3] if len(tail) > 3 else "",
        }
    return items


def parse_duty_pages(pages):
    """Parse Part III pages (2 items per page typically). Returns dict
    itemsn(str) -> flat field dict combining info + 3 duty blocks."""
    items = {}
    current_item_fields = {}
    current_itemsn = None
    for page in pages:
        rows = get_rows(page)
        blocks = parse_label_value_blocks(rows)
        for h, v in blocks:
            has_duty_marker = any((c or "").strip() == "DUTY" for c in h)
            if has_duty_marker:
                f = block_to_duty_fields(h, v)
            else:
                f = block_to_simple_fields(h, v)
            if not f:
                continue
            if "ITEMSN" in f:
                # start of a new item's info block -> flush previous
                if current_itemsn is not None:
                    items[current_itemsn] = current_item_fields
                current_itemsn = f.get("ITEMSN")
                current_item_fields = dict(f)
            else:
                current_item_fields.update(f)
    if current_itemsn is not None:
        items[current_itemsn] = current_item_fields
    return items


SECTION_TITLE_RE = re.compile(r"^([A-Z])\.\s*([A-Za-z][A-Za-z0-9 &/\-\.,]+)$")


def _is_section_title_row(row):
    for c in row:
        if not c:
            continue
        v = clean_cell(c)
        m = SECTION_TITLE_RE.match(v)
        if m:
            return m.group(2).strip()
    return None


def parse_part4_pages(pages):
    """Parse Part IV (Additional Details) pages: a series of lettered
    sections (A. SVB DETAILS, B. PREVIOUS BEs, ... J. SINGLE WINDOW
    DECLARATION, M. SUPPORTING DOCUMENTS, etc.), each a flat table keyed
    by (INVSNO, ITMSNO). Returns (item_extra, invoice_extra, all_section_cols)
    where item_extra: {itemsn(str) -> {field: value}}, invoice_extra:
    {field: value} for rows tagged ITMSNO=0 or with no item column, and
    all_section_cols: {section_name: [labels]} so every item can be given
    a consistent (possibly blank) set of columns.
    """
    item_extra = {}
    invoice_extra = {}
    all_section_cols = {}
    # Licence-type sections (e.g. "LICENCE DETAILS") can have several rows
    # per item (one per licence). Those are collected here as whole-row
    # entries and combined into a single "N) label: val, ..." line per
    # licence in one cell, instead of one column per label (#see build_wide_row).
    item_licence_entries = {}
    invoice_licence_entries = {}

    current_section = None
    label_cols = None  # list of (col_idx, label)
    itmsno_col = None
    section_header_cache = {}

    for page in pages:
        rows = get_rows(page)
        for row in rows:
            title = _is_section_title_row(row)
            if title:
                current_section = title
                label_cols = section_header_cache.get(title)
                itmsno_col = None
                if label_cols:
                    for idx, label in label_cols:
                        if re.sub(r"[^A-Z]", "", label.upper()) in ("ITMSNO", "ITEMSN"):
                            itmsno_col = idx
                continue
            if current_section is None:
                continue
            if is_header_row(row):
                label_cols = [
                    (idx, strip_label(c)) for idx, c in enumerate(row) if strip_label(c)
                ]
                section_header_cache[current_section] = label_cols
                # normalize label for ITMSNO detection (ITMSNO / ITEMSN / ITMSNO)
                itmsno_col = None
                for idx, label in label_cols:
                    if re.sub(r"[^A-Z]", "", label.upper()) in ("ITMSNO", "ITEMSN"):
                        itmsno_col = idx
                all_section_cols.setdefault(current_section, [l for _, l in label_cols])
                continue
            if label_cols is None:
                continue
            values = [clean_cell(c) for c in row]
            if not any(values):
                continue
            data = {}
            for idx, label in label_cols:
                data[label] = values[idx] if idx < len(values) else ""
            itmsno = data.get("ITMSNO") or data.get("ITEMSN") or ""
            target = None
            if itmsno and re.match(r"^\d+$", itmsno) and itmsno != "0":
                target = item_extra.setdefault(itmsno, {})

            is_licence_section = (
                "LICENCE" in current_section.upper() or "LICENSE" in current_section.upper()
            )
            if is_licence_section:
                entry_parts = [
                    f"{label}: {val}" for label, val in data.items()
                    if label.upper() not in ("INVSNO", "INVSN", "ITMSNO", "ITEMSN") and val
                ]
                if entry_parts:
                    entry_str = "; ".join(entry_parts)
                    if target is not None:
                        item_licence_entries.setdefault(current_section, {}) \
                            .setdefault(itmsno, []).append(entry_str)
                    else:
                        invoice_licence_entries.setdefault(current_section, []).append(entry_str)
                continue

            for label, val in data.items():
                if label.upper() in ("INVSNO", "INVSN", "ITMSNO", "ITEMSN"):
                    continue
                if not val:
                    continue
                key = f"{current_section}_{label}"
                if target is not None:
                    if target.get(key):
                        target[key] = target[key] + "; " + val
                    else:
                        target[key] = val
                else:
                    if invoice_extra.get(key):
                        invoice_extra[key] = invoice_extra[key] + "; " + val
                    else:
                        invoice_extra[key] = val

    # Combine each item's licence rows into one cell, one licence per line.
    for section, by_item in item_licence_entries.items():
        for itmsno, entries in by_item.items():
            combined = (
                "\n".join(f"{i}) {e}" for i, e in enumerate(entries, start=1))
                if len(entries) > 1 else entries[0]
            )
            item_extra.setdefault(itmsno, {})[section] = combined

    for section, entries in invoice_licence_entries.items():
        if entries:
            invoice_extra[section] = (
                "\n".join(f"{i}) {e}" for i, e in enumerate(entries, start=1))
                if len(entries) > 1 else entries[0]
            )

    return item_extra, invoice_extra, all_section_cols


def parse_boe(path):
    """Parse a full ICEGATE BOE PDF into:
      - header: dict of BOE-level fields (Part I)
      - invoice: dict of invoice/valuation-level fields (Part II, excl. items)
      - items: dict itemsn -> combined flat field dict (Part II item row + Part III duties)
    """
    with pdfplumber.open(path) as pdf:
        sections = _identify_sections(pdf)
        part1_pages = [pdf.pages[i] for i in sections.get("I", [])]
        part2_pages = [pdf.pages[i] for i in sections.get("II", [])]
        part3_pages = [pdf.pages[i] for i in sections.get("III", [])]

        header = {}
        for p in part1_pages:
            header.update(extract_simple_fields(get_rows(p)))
        if part1_pages:
            top_box = parse_top_box(part1_pages[0])
            for k, v in top_box.items():
                if v:
                    header[k] = v

        invoice_rows_all = []
        for p in part2_pages:
            invoice_rows_all.extend(get_rows(p))
        invoice = extract_simple_fields(invoice_rows_all)
        item_basic = parse_item_table(invoice_rows_all)

        item_duty = parse_duty_pages(part3_pages)

        part4_pages = [pdf.pages[i] for i in sections.get("IV", [])]
        item_extra, invoice_extra, all_section_cols = parse_part4_pages(part4_pages)
        invoice.update(invoice_extra)

        # merge basic item info + duty info per item, keyed by SNo/ITEMSN
        items = {}
        all_sno = set(item_basic.keys()) | set(item_duty.keys()) | set(item_extra.keys())
        for sno in sorted(all_sno, key=lambda x: int(x) if x.isdigit() else 0):
            merged = {}
            merged.update(item_basic.get(sno, {}))
            merged.update(item_duty.get(sno, {}))
            # ensure every Part IV section's columns exist for every item
            # (blank if this item had no data there), so licence/SVB/etc.
            # columns are always present and ready to populate.
            for section, labels in all_section_cols.items():
                if "LICENCE" in section.upper() or "LICENSE" in section.upper():
                    # Licence rows are combined into one cell keyed by the
                    # section name itself (see parse_part4_pages), not one
                    # column per original label.
                    merged.setdefault(section, "")
                    continue
                for label in labels:
                    if label.upper() in ("INVSNO", "INVSN", "ITMSNO", "ITEMSN"):
                        continue
                    merged.setdefault(f"{section}_{label}", "")
            merged.update(item_extra.get(sno, {}))
            items[sno] = merged

        return {"header": header, "invoice": invoice, "items": items}


def build_wide_row(parsed):
    """Flatten parsed BOE dict into a single wide row dict suitable for
    a 1-row-per-BOE Excel export. "BOE No" and "Shipper Name" are placed
    first (dict order = column order) since they're the two fields the
    export is meant to be scanned/sorted by."""
    header = parsed["header"]
    row = {
        "BOE No": header.get("BE No", ""),
        "Shipper Name": shipper_name(header),
    }
    for k, v in parsed["header"].items():
        row[f"BOE_{k}"] = v
    for k, v in parsed["invoice"].items():
        row[f"Invoice_{k}"] = v
    for sno, fields in parsed["items"].items():
        n = sno
        for k, v in fields.items():
            row[f"Item{n}_{k}"] = v
    return row


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads/HK_JADE_USD_18_204_78_BOE.pdf"
    with pdfplumber.open(path) as pdf:
        rows = get_rows(pdf.pages[0])
        fields = extract_simple_fields(rows)
        for k, v in fields.items():
            print(f"{k!r}: {v!r}")
