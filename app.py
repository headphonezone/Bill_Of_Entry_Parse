import io
import re
import tempfile
import os

import pandas as pd
import streamlit as st
from openpyxl.styles import Alignment

from boe_parser import parse_boe, build_wide_row

st.set_page_config(page_title="BOE to Excel (Single Row)", page_icon="📄", layout="wide")

st.title("📄 Bill of Entry → Excel (Single Row per BOE)")
st.caption(
    "Upload an ICEGATE-format Bill of Entry PDF. All header, invoice, and "
    "per-item duty details are flattened into ONE Excel row, with each "
    "item's fields repeated as Item1_*, Item2_*, ... columns."
)

uploaded = st.file_uploader("Upload BOE PDF", type=["pdf"])

if uploaded is not None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(uploaded.read())
        tmp_path = tmp.name

    try:
        with st.spinner("Parsing BOE PDF... this can take a few seconds for 20+ page BOEs."):
            parsed = parse_boe(tmp_path)
            row = build_wide_row(parsed)
    except Exception as e:
        st.error(f"Failed to parse this PDF: {e}")
        st.stop()
    finally:
        os.unlink(tmp_path)

    n_items = len(parsed["items"])
    be_no = row.get("BOE_BE No", "")
    be_date = row.get("BOE_BE Date", "")
    total_duty = row.get("BOE_TOTAL DUTY", "")
    ass_val = row.get("BOE_TOT.ASS VAL", "")

    st.success(f"Parsed BE No **{be_no}** ({be_date}) — {n_items} line items detected.")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("BE Number", be_no or "—")
    col2.metric("Items", n_items)
    col3.metric("Assessable Value", ass_val or "—")
    col4.metric("Total Duty", total_duty or "—")

    with st.expander("Header (Part I) fields", expanded=False):
        st.dataframe(
            pd.DataFrame(list(parsed["header"].items()), columns=["Field", "Value"]),
            use_container_width=True,
            height=300,
        )

    with st.expander("Invoice / Valuation (Part II) fields", expanded=False):
        st.dataframe(
            pd.DataFrame(list(parsed["invoice"].items()), columns=["Field", "Value"]),
            use_container_width=True,
            height=300,
        )

    with st.expander(f"Item-level details ({n_items} items)", expanded=True):
        item_rows = []
        for sno, fields in parsed["items"].items():
            r = {"Item #": sno}
            r.update(fields)
            item_rows.append(r)
        items_df = pd.DataFrame(item_rows)
        st.dataframe(items_df, use_container_width=True, height=400)

    # Build the single-row wide dataframe
    wide_df = pd.DataFrame([row])

    st.subheader("Single-row export preview")
    st.write(f"This BOE will export as **1 row × {len(row)} columns**.")
    st.dataframe(wide_df.iloc[:, :20], use_container_width=True)
    st.caption("(showing first 20 of the full column set — download for the complete row)")

    # Excel export
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        wide_df.to_excel(writer, index=False, sheet_name="BOE")
        items_df.to_excel(writer, index=False, sheet_name="Items (reference)")

        # Cells like licence details hold multiple "N) ..." lines separated
        # by \n (see build_wide_row / parse_part4_pages) - turn on wrap text
        # so they render as stacked lines instead of one run-on line.
        ws = writer.sheets["BOE"]
        max_lines = 1
        for col_idx, col_name in enumerate(wide_df.columns, start=1):
            if wide_df[col_name].astype(str).str.contains("\n").any():
                for row_idx in range(2, ws.max_row + 1):
                    cell = ws.cell(row=row_idx, column=col_idx)
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
                    max_lines = max(max_lines, str(cell.value or "").count("\n") + 1)
        if max_lines > 1:
            for row_idx in range(2, ws.max_row + 1):
                ws.row_dimensions[row_idx].height = 15 * max_lines
    buf.seek(0)

    safe_be = re.sub(r"[^A-Za-z0-9_-]", "_", str(be_no) or "BOE")
    st.download_button(
        label="⬇️ Download Excel (single row)",
        data=buf,
        file_name=f"BOE_{safe_be}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

else:
    st.info("Upload a BOE PDF to begin.")
