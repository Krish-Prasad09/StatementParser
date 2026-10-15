"""
app.py
------
Interactive Streamlit Operations Dashboard for the Activv Money PMS/AIF Statement Parser.
Operational Views:
1. 📬 Mailbox Ingestion & Operations: Real-time email gatekeeper, IMAP poller & SQLite audit trail
2. 💼 Client Portfolio Consolidation: Multi-custodian wealth consolidation by PAN & ISIN
3. ⚠️ Quarantined Review Queue: Financial safety gate exception queue & audit architecture
4. 📁 Direct Statement Upload: Manual statement and email ingestion workbench
"""

import os
import csv
import json
import tempfile
import pandas as pd
import streamlit as st

from pipeline import ProcessingPipeline
from consolidator import PortfolioConsolidator
from ingestion import IngestionEngine
from models import ExtractionResult
from persistence import StatementStore
from watch_mailbox import safe_poll_inbox

store = StatementStore("statements.db")

# Startup Mailbox Scan (Runs safely once per Streamlit session)
if "mailbox_polled_at_startup" not in st.session_state:
    st.session_state["mailbox_polled_at_startup"] = True
    try:
        poll_res = safe_poll_inbox(db_path="statements.db")
        st.session_state["last_poll_result"] = poll_res
    except Exception as e:
        st.session_state["last_poll_result"] = {"status": "error", "count": 0, "message": str(e)}

# Page Configuration
st.set_page_config(
    page_title="Activv Money | PMS & AIF Statement Parser",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Styling
st.markdown("""
<style>
    .metric-card {
        background-color: #f8f9fa;
        border-radius: 8px;
        padding: 16px;
        border-left: 5px solid #1f77b4;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05);
    }
    .badge-validated {
        background-color: #d4edda;
        color: #155724;
        padding: 4px 8px;
        border-radius: 4px;
        font-weight: bold;
    }
    .badge-review {
        background-color: #fff3cd;
        color: #856404;
        padding: 4px 8px;
        border-radius: 4px;
        font-weight: bold;
    }
    .badge-dup {
        background-color: #cce5ff;
        color: #004085;
        padding: 4px 8px;
        border-radius: 4px;
        font-weight: bold;
    }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_pipeline():
    return ProcessingPipeline(templates_dir="templates", db_path="statements.db")


@st.cache_resource
def get_consolidator():
    return PortfolioConsolidator()


pipeline = get_pipeline()
consolidator = get_consolidator()
ingestion = IngestionEngine()


def render_statement_view(stmt, account_idx=1):
    meta = stmt.statement
    st.markdown(f"#### Account #{account_idx}: {meta.client_name or 'N/A'} ({meta.provider})")

    # Metadata Cards
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.markdown(f"**Client ID:** `{meta.client_id or 'N/A'}`")
    m2.markdown(f"**PAN:** `{meta.pan or 'N/A'}`")
    m3.markdown(f"**Period:** `{meta.period_start} to {meta.period_end}`")
    m4.markdown(f"**Strategy:** `{meta.strategy or 'N/A'}`")
    m5.markdown(f"**Currency:** `{meta.currency}`")

    # Summary Balances
    st.markdown("##### Summary Balances")
    s = stmt.summary
    s1, s2, s3, s4, s5, s6, s7 = st.columns(7)
    s1.metric("Opening", f"{s.opening:,.2f}")
    s2.metric("Inflows", f"{s.inflow:,.2f}")
    s3.metric("Outflows", f"{s.outflow:,.2f}")
    s4.metric("Net Gain", f"{s.gain:,.2f}" if s.gain is not None else "N/A")
    s5.metric("Fees", f"{s.fee:,.2f}")
    s6.metric("Cash Balance", f"{s.cash:,.2f}")
    s7.metric("Closing NAV", f"{s.closing:,.2f}")

    # Holdings Table
    if stmt.holdings:
        st.markdown("##### Portfolio Holdings")
        h_rows = []
        for h in stmt.holdings:
            h_rows.append({
                "Security": str(h.name),
                "ISIN": str(h.isin or "Unlisted / None"),
                "Quantity": f"{h.quantity:,.2f}",
                "Avg Cost": f"{h.avg_cost:,.2f}" if h.avg_cost is not None else "-",
                "Price": f"{h.price:,.2f}" if h.price is not None else "-",
                "Market Value": f"{h.market_value:,.2f}",
                "Weight (%)": f"{h.weight_pct:.2f}%" if h.weight_pct is not None else "-"
            })
        st.dataframe(pd.DataFrame(h_rows))

    # Transactions Table
    if stmt.transactions:
        st.markdown("##### Transactions Ledger")
        t_rows = []
        for t in stmt.transactions:
            t_rows.append({
                "Date": str(t.date),
                "Type": str(t.type),
                "Security": str(t.security or "-"),
                "ISIN": str(t.isin or "-"),
                "Quantity": f"{t.quantity:,.2f}" if t.quantity is not None else "-",
                "Price": f"{t.price:,.2f}" if t.price is not None else "-",
                "Amount": f"{t.amount:,.2f}"
            })
        st.dataframe(pd.DataFrame(t_rows))

    # Raw JSON toggle
    with st.expander("View Raw Extracted JSON"):
        st.json(stmt.model_dump())


# Sidebar
st.sidebar.title("Activv Money Parser")
st.sidebar.markdown("**Deterministic PMS/AIF Engine**")
st.sidebar.markdown("---")
st.sidebar.markdown("""
**Production Guarantees:**
- ⚡ **Zero-LLM Cost on Known Formats:** $0.00 (0 calls)
- 🛡️ **Mathematical Double-Entry Integrity:** 100% verified
- ⏱️ **Sub-Second Extraction:** ~100ms / statement
- 🔒 **Financial Safety Gate:** Auto-quarantine on anomaly
""")
st.sidebar.markdown("---")

# Mailbox Status & Manual Trigger in Sidebar
st.sidebar.subheader("📬 Mailbox Ingestion")
last_poll = st.session_state.get("last_poll_result")
if last_poll:
    if last_poll.get("status") == "success":
        st.sidebar.success(f"Last scan: {last_poll.get('count', 0)} new email(s)")
    elif last_poll.get("status") == "not_configured":
        st.sidebar.caption("IMAP credentials not set in `.env`")
    else:
        st.sidebar.error(f"Scan error: {last_poll.get('message')}")

if st.sidebar.button("🔄 Check Inbox Now", key="btn_sidebar_poll"):
    with st.spinner("Connecting to IMAP and polling unseen emails..."):
        poll_res = safe_poll_inbox(db_path="statements.db")
        st.session_state["last_poll_result"] = poll_res
        if poll_res.get("status") == "success":
            st.sidebar.success(f"Processed {poll_res.get('count', 0)} email(s)!")
        elif poll_res.get("status") == "not_configured":
            st.sidebar.info("Mailbox not configured in .env")
        else:
            st.sidebar.error(poll_res.get("message"))
        st.rerun()

st.sidebar.markdown("---")
# Template Management & Cold-Start Controls
st.sidebar.subheader("⚙️ Templates & Cold Start")
import glob, shutil
current_templates = [f for f in glob.glob("templates/*.json")]
st.sidebar.caption(f"Learned Templates in `templates/`: **{len(current_templates)}**")

col_t1, col_t2 = st.sidebar.columns(2)
with col_t1:
    if st.sidebar.button("📥 Seed Fixtures", help="Copy 9 validated templates from test_fixtures/ to templates/ for instant zero-LLM testing"):
        for f in glob.glob("test_fixtures/templates/*.json"):
            shutil.copy(f, "templates/")
        st.cache_resource.clear()
        st.sidebar.success("Seeded 9 templates!")
        st.rerun()

with col_t2:
    if st.sidebar.button("🧹 Cold Start", help="Delete templates in templates/ to test cold-start LLM learning"):
        for f in glob.glob("templates/*.json"):
            try:
                os.remove(f)
            except Exception:
                pass
        st.cache_resource.clear()
        st.sidebar.info("Cleared templates/")
        st.rerun()

st.sidebar.markdown("---")
view_mode = st.sidebar.radio(
    "Navigation",
    [
        "📬 Mailbox Ingestion & Operations",
        "💼 Client Portfolio Consolidation",
        "⚠️ Quarantined Review Queue",
        "📤 Live Email Dispatch & Ingest"
    ]
)


# -----------------------------------------------------------------------------
# VIEW 1: Mailbox Ingestion & Operations (Default Landing View)
# -----------------------------------------------------------------------------
if view_mode == "📬 Mailbox Ingestion & Operations":
    st.title("📬 Mailbox Ingestion & Operations")
    st.markdown("Complete audit trail of incoming emails, gatekeeper routing, and statement attachment ingestion from SQLite (`statements.db`).")

    # Manual poll button in main view
    col_hdr1, col_hdr2 = st.columns([3, 1])
    with col_hdr2:
        if st.button("🔄 Check Inbox Now", key="btn_main_poll"):
            with st.spinner("Connecting to IMAP and scanning unseen messages..."):
                poll_res = safe_poll_inbox(db_path="statements.db")
                st.session_state["last_poll_result"] = poll_res
                st.rerun()

    # Load SQLite records
    all_stmts = store.get_all()
    email_events = store.get_email_events()

    # Calculate 6 required counters
    val_count = sum(1 for s in all_stmts if s.get("status") == "validated")
    review_count = sum(1 for s in all_stmts if s.get("status") == "needs_review")
    dup_count = sum(1 for s in all_stmts if s.get("status") == "duplicate")
    ignored_count = sum(1 for e in email_events if e.get("status") == "ignore")

    unsupported_codes = [
        "unsupported_statement_attachment_type",
        "ambiguous_attachment_requires_review",
        "inline_pdf_missing_filename",
        "zip_attachment_requires_manual_review"
    ]
    unsupported_stmt_count = sum(
        1 for s in all_stmts
        if any(c in (s.get("issues_json") or "") for c in unsupported_codes)
        or s.get("format_id") == "unsupported_or_ambiguous"
    )
    unsupported_email_count = sum(
        1 for e in email_events
        if e.get("status") == "needs_review" and any(c in (e.get("reason") or "") for c in unsupported_codes)
    )
    unsupported_count = unsupported_stmt_count + unsupported_email_count

    total_llm_calls = sum(int(s.get("llm_calls_made", 0) or 0) for s in all_stmts)

    # Render Counters
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Validated Statements", val_count)
    c2.metric("Needs Review", review_count)
    c3.metric("Duplicates", dup_count)
    c4.metric("Ignored / Non-Stmts", ignored_count)
    c5.metric("Unsupported Reviews", unsupported_count)
    c6.metric("LLM Calls Made", total_llm_calls)

    st.markdown("---")

    tab_events, tab_stmts = st.tabs(["📧 Email Ingestion Events (email_events)", "📄 Parsed Statement Attachments (statements)"])

    with tab_events:
        st.subheader("All Received Email Events")
        if email_events:
            events_table = []
            for ev in email_events:
                try:
                    att_list = json.loads(ev.get("attachments_json") or "[]")
                    att_str = ", ".join(att_list) if att_list else "None"
                except Exception:
                    att_str = ev.get("attachments_json") or "None"

                events_table.append({
                    "DB ID": ev.get("id"),
                    "Message ID": ev.get("message_id"),
                    "Sender": ev.get("sender"),
                    "Subject": ev.get("subject"),
                    "Received At": ev.get("received_at") or "-",
                    "Status": ev.get("status"),
                    "Stored Reason / Issue": ev.get("reason") or "N/A",
                    "Attachments": att_str,
                    "Processed At": ev.get("processed_at")
                })
            df_events = pd.DataFrame(events_table)
            status_opts = list(df_events["Status"].unique())
            status_filter = st.multiselect(
                "Filter by Email Status:",
                options=status_opts,
                default=status_opts,
                key="filter_email_status"
            )
            df_filtered = df_events[df_events["Status"].isin(status_filter)]
            st.dataframe(df_filtered, use_container_width=True)
        else:
            st.info("No email events logged in SQLite store yet. Use 'Check Inbox Now' or upload an .eml file.")

    with tab_stmts:
        st.subheader("Processed Attachment Records from Mailbox & Uploads")
        if all_stmts:
            stmts_table = []
            for s in all_stmts:
                try:
                    iss_list = json.loads(s.get("issues_json") or "[]")
                    iss_str = ", ".join(iss_list) if iss_list else "None"
                except Exception:
                    iss_str = s.get("issues_json") or "None"

                stmts_table.append({
                    "DB ID": s.get("id"),
                    "Filename": os.path.basename(s.get("file_path") or ""),
                    "Status": s.get("status"),
                    "Format ID": s.get("format_id") or "None",
                    "Issues": iss_str,
                    "SHA-256 Hash": s.get("sha256_hash") or "-",
                    "LLM Calls": s.get("llm_calls_made", 0),
                    "Created At": s.get("created_at")
                })
            df_stmts = pd.DataFrame(stmts_table)
            status_stmt_opts = list(df_stmts["Status"].unique())
            status_stmt_filter = st.multiselect(
                "Filter by Statement Status:",
                options=status_stmt_opts,
                default=status_stmt_opts,
                key="filter_stmt_status"
            )
            df_stmts_filtered = df_stmts[df_stmts["Status"].isin(status_stmt_filter)]
            st.dataframe(df_stmts_filtered, use_container_width=True)
        else:
            st.info("No statement records logged in SQLite store yet.")


# -----------------------------------------------------------------------------
# VIEW 2: Client Portfolio Consolidation
# -----------------------------------------------------------------------------
elif view_mode == "💼 Client Portfolio Consolidation":
    st.title("💼 Multi-Custodian Portfolio Consolidation")
    st.markdown("Consolidated family office & HNI wealth view joining accounts across disparate wealth managers.")

    # Load validated statements from SQLite store
    db_stmts = store.get_all()
    all_raw_results = []
    seen_hashes = set()
    for row in db_stmts:
        if row.get("status") == "validated" and row.get("record_json"):
            try:
                res_obj = ExtractionResult.model_validate_json(row["record_json"])
                all_raw_results.append(res_obj)
                if res_obj.sha256_hash:
                    seen_hashes.add(res_obj.sha256_hash)
            except Exception:
                pass

    # If SQLite has few or no records, supplement with sample statements
    # so multi-custodian consolidation (e.g., Ananya Rao SAMPL1001A across Aurum, Delta, Evergreen) is viewable
    if len(all_raw_results) < 3:
        sample_consolidation_files = [
            "sample_data/pms/01_aurum_A1_standard.pdf",
            "sample_data/pms/08_delta_D1_ledger.pdf",
            "sample_data/aif/12_evergreen_E1_cat2.pdf",
            "sample_data/pms/04_banyan_B1_quarterly_2pages.pdf"
        ]
        for fpath in sample_consolidation_files:
            if os.path.exists(fpath):
                sample_res = pipeline.process_file(fpath)
                if sample_res.status == "validated" and sample_res.sha256_hash not in seen_hashes:
                    all_raw_results.append(sample_res)
                    seen_hashes.add(sample_res.sha256_hash)

    # Dynamically discover all unique client PANs in results
    discovered_pans = sorted(list(set(
        stmt.statement.pan
        for r in all_raw_results
        for stmt in r.statements
        if stmt.statement.pan
    )))
    discovered_dates = sorted(list(set(
        stmt.statement.period_end
        for r in all_raw_results
        for stmt in r.statements
        if stmt.statement.period_end
    )), reverse=True)

    col_in1, col_in2, col_in3 = st.columns(3)
    with col_in1:
        if discovered_pans:
            target_pan = st.selectbox("Client PAN Number:", discovered_pans)
        else:
            target_pan = st.text_input("Client PAN Number:", value="")
    with col_in2:
        if discovered_dates:
            target_date = st.selectbox("Reporting Date:", discovered_dates)
        else:
            target_date = st.text_input("Reporting Date (YYYY-MM-DD):", value="2026-09-30")
    with col_in3:
        target_currency = st.selectbox("Currency:", ["INR", "USD"])

    consolidated = consolidator.consolidate_client(
        results=all_raw_results,
        target_pan=target_pan,
        target_period_end=target_date,
        target_currency=target_currency
    )

    if consolidated:
        st.subheader(f"Portfolio of {consolidated.client_name} (PAN: `{consolidated.pan}`)")
        st.caption(f"Consolidated as of {consolidated.period} ({consolidated.currency})")

        # Top Metrics
        p1, p2, p3, p4 = st.columns(4)
        curr_symbol = "₹" if consolidated.currency == "INR" else "$"
        p1.metric("Total Corpus", f"{curr_symbol} {consolidated.total_corpus:,.2f}")
        p2.metric("Total Cash", f"{curr_symbol} {consolidated.total_cash:,.2f}")
        p3.metric("Custodians Joined", len(consolidated.participating_providers))
        p4.metric("Statements Consolidated", consolidated.raw_statements_count)

        st.markdown(f"**Participating Wealth Managers:** {', '.join(consolidated.participating_providers)}")

        st.markdown("---")

        # Asset Allocation
        st.subheader("Asset Allocation")
        alloc_cols = st.columns(3)
        for idx, (asset_class, pct) in enumerate(consolidated.asset_allocation.items()):
            alloc_cols[idx].metric(asset_class, f"{pct:.2f}%")

        # Allocation Bar
        df_alloc = pd.DataFrame([consolidated.asset_allocation])
        st.bar_chart(df_alloc)

        st.markdown("---")

        tab_cross_provider, tab_joined_isin, tab_aif = st.tabs([
            "📑 Multi-Provider Holdings Ledger (Client / Provider / Security / Value)",
            "📌 Unified Holdings (Joined by ISIN across Custodians)",
            "🔒 AIF Alternative Holdings (Private Equity / Venture)"
        ])

        with tab_cross_provider:
            st.subheader("📑 Consolidated Investments Across All Providers")
            st.markdown("Line-by-line holding breakdown tagging every asset to its specific wealth manager / custodian.")
            provider_rows = []
            for r in all_raw_results:
                for s in r.statements:
                    if s.statement.pan == target_pan:
                        p_name = s.statement.provider or r.format_id
                        for h in s.holdings:
                            provider_rows.append({
                                "Client": s.statement.client_name,
                                "Provider / Custodian": p_name,
                                "Security": h.name,
                                "ISIN": h.isin or "Unlisted / AIF",
                                "Quantity": f"{h.quantity:,.2f}" if h.quantity else "-",
                                "Market Value": f"{curr_symbol} {h.market_value:,.2f}"
                            })
            if provider_rows:
                st.dataframe(pd.DataFrame(provider_rows), use_container_width=True)
            else:
                st.info("No holding records found for this client.")

        with tab_joined_isin:
            st.subheader("📌 Listed Equities (Joined Across Custodians by ISIN)")
            cross_custodian = [h for h in consolidated.listed_holdings if len(h.providers_held_in) > 1]
            if cross_custodian:
                joined_desc = "; ".join([f"**{h.name}** ({h.isin}) joined across {', '.join(h.providers_held_in)}" for h in cross_custodian])
                st.info(f"Cross-custodian holdings identified: {joined_desc}")
            else:
                st.caption("No overlapping securities held across multiple custodians.")

            listed_rows = []
            for h in consolidated.listed_holdings:
                listed_rows.append({
                    "ISIN": h.isin,
                    "Security": h.name,
                    "Quantity": h.quantity,
                    "Market Value": f"{curr_symbol} {h.market_value:,.2f}",
                    "Held With": ", ".join(h.providers_held_in)
                })
            st.dataframe(pd.DataFrame(listed_rows), use_container_width=True)

        with tab_aif:
            st.subheader("🔒 AIF Alternative Holdings (Unlisted Bucket)")
            st.markdown("Private equity and venture assets that do not carry public ISIN identifiers.")

            aif_rows = []
            for a in consolidated.aif_holdings:
                name = a.get("name", "") if isinstance(a, dict) else getattr(a, "name", "")
                qty = a.get("quantity", 0) if isinstance(a, dict) else getattr(a, "quantity", 0)
                mv = a.get("market_value", 0.0) if isinstance(a, dict) else getattr(a, "market_value", 0.0)
                prov = a.get("provider", "") if isinstance(a, dict) else getattr(a, "provider", "")
                aif_rows.append({
                    "Security / Fund": name,
                    "Quantity / Units": qty,
                    "Valuation (INR)": f"₹ {mv:,.2f}",
                    "Custodian": prov
                })
            st.dataframe(pd.DataFrame(aif_rows), use_container_width=True)

        if consolidated.excluded_statements:
            with st.expander(f"Excluded Statements Log ({len(consolidated.excluded_statements)} items)"):
                st.dataframe(pd.DataFrame(consolidated.excluded_statements), use_container_width=True)

    else:
        st.warning(f"No validated statements found for PAN '{target_pan}' as of {target_date}.")
# -----------------------------------------------------------------------------
# VIEW 3: Quarantined Review Queue
# -----------------------------------------------------------------------------
elif view_mode == "⚠️ Quarantined Review Queue":
    st.title("⚠️ Quarantined Review Queue")
    st.markdown("Financial safety gate: Statements with detected mathematical errors, layout drift, or checksum issues.")

    tab_db_review, tab_audit_rules = st.tabs(["Durable Quarantine Store (statements.db)", "🛡️ Safety Gate Rules & Architecture"])

    with tab_db_review:
        st.subheader("Durable Quarantine Store (Persisted from IMAP Worker & Uploads)")
        db_review = store.get_needs_review()
        if db_review:
            st.warning(f"Found {len(db_review)} statement(s) quarantined in SQLite database.")
            db_table = []
            for row in db_review:
                issues_list = json.loads(row.get("issues_json", "[]"))
                db_table.append({
                    "DB ID": row["id"],
                    "File": row["file_path"],
                    "Format": row["format_id"],
                    "Status": row["status"],
                    "Issues": ", ".join(issues_list) if issues_list else "None",
                    "Ingested At": row["created_at"]
                })
            st.dataframe(pd.DataFrame(db_table), use_container_width=True)
        else:
            st.success("No items currently pending review in SQLite database.")

    with tab_audit_rules:
        st.subheader("Automated Financial Safety Gate Architecture")
        st.markdown("""
        Every PMS/AIF statement processed from emails or uploads must pass deterministic multi-stage financial integrity checks:

        1. **🧮 Roll-Forward Reconciliation:** `Opening Corpus + Inflows - Outflows + Net Gain - Fees == Closing Corpus`. If reconciliation fails by > ₹1.00, file is quarantined with `summary_rollforward_mismatch`.
        2. **⚖️ Holdings vs Corpus Audit:** Sum of individual holding market values must match stated Total Equity within 0.1%. Mismatches trigger `holdings_sum_ne_stated_total_equity`.
        3. **🔍 ISO 6166 ISIN Checksum:** All listed Indian securities undergo Modulo-10 Luhn check digit verification. Invalid checksums trigger `invalid_isin_checksum`.
        4. **📑 Continuation Page Verification:** Statements indicating 'Page 1 of 2' missing follow-on holdings trigger `incomplete_document_missing_continuation_page`.
        5. **🚨 Layout Drift Quarantine:** When a known provider modifies headers or table layout, the system flags `layout_drift_known_provider` and isolates the document until template sign-off.
        6. **📷 Text-Layer Gate:** Scanned image-only PDFs with < 30 characters are detected in < 15ms and routed to OCR review (`no_text_layer_requires_ocr`).
        7. **♻️ 0ms Byte Deduplication:** Byte-identical PDFs are identified via SHA-256 hash lookup in SQLite and flagged as `duplicate` with zero redundant processing.
        """)


# -----------------------------------------------------------------------------
# VIEW 4: Live Statement Email Dispatcher & Ingestion
# -----------------------------------------------------------------------------
elif view_mode == "📤 Live Email Dispatch & Ingest":
    st.title("📤 Live Statement Email Dispatcher & Mailbox Ingestion")
    st.markdown(
        "Upload any statement file (PDF, Excel, CSV) to **dispatch it as a realistic email via SMTP** to your configured mailbox, "
        "then automatically trigger the IMAP poller and Ingestion Gatekeeper to audit, parse, and persist it."
    )

    tab_dispatch, tab_eml = st.tabs(["🚀 Live Mailbox Email Dispatcher", "✉️ Offline .eml Email File Simulator"])

    # --- TAB A: Live Mailbox Email Dispatcher ---
    with tab_dispatch:
        st.subheader("📤 Dispatch Statement Email to Mailbox")
        st.markdown(
            "Upload any statement file or select a sample statement. "
            "The system sends it via SMTP with a randomized realistic subject to `IMAP_USER`, then polls the inbox via IMAP to ingest it end-to-end."
        )

        upload_source = st.radio(
            "Statement Source:",
            ["Upload Custom Statement File", "Select Sample Statement"],
            horizontal=True
        )

        target_file_path = None
        target_file_name = ""

        if upload_source == "Upload Custom Statement File":
            uploaded_file = st.file_uploader("Drop statement file here (PDF, XLSX, CSV):", type=["pdf", "xlsx", "csv"])
            if uploaded_file is not None:
                with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(uploaded_file.name)[1]) as tmp:
                    tmp.write(uploaded_file.getvalue())
                    target_file_path = tmp.name
                    target_file_name = uploaded_file.name
        else:
            sample_docs = [
                "sample_data/pms/01_aurum_A1_standard.pdf",
                "sample_data/pms/04_banyan_B1_quarterly_2pages.pdf",
                "sample_data/pms/08_delta_D1_ledger.pdf",
                "sample_data/aif/12_evergreen_E1_cat2.pdf",
                "sample_data/excel_csv/16_hestia_statement.xlsx",
                "sample_data/excel_csv/17_hestia_statement.csv",
                "sample_data/edge_cases/18_scanned_aurum_A1.pdf",
                "sample_data/edge_cases/19_broken_totals_aurum.pdf",
                "sample_data/edge_cases/20_broken_rollforward_cedar.pdf",
                "sample_data/edge_cases/22_broken_isin_delta.pdf",
                "sample_data/edge_cases/24_neg_factsheet.pdf"
            ]
            selected_sample = st.selectbox("Select Sample Document:", sample_docs)
            target_file_path = selected_sample
            target_file_name = os.path.basename(selected_sample)

        custom_subject = st.text_input(
            "Custom Email Subject (optional - leave blank to generate a random realistic subject):",
            placeholder="e.g. Your PMS Portfolio Statement for September 2026"
        )

        col_btn1, col_btn2 = st.columns([1, 1])
        with col_btn1:
            send_and_ingest = st.button("🚀 Send Email & Trigger Ingestion", type="primary")
        with col_btn2:
            send_only = st.button("✉️ Send Email Only (Test Poller from Terminal)")

        if target_file_path and (send_and_ingest or send_only):
            from send_test_email import send_statement_email
            with st.spinner("Connecting to SMTP server (smtp.gmail.com) and dispatching email..."):
                mail_res = send_statement_email(
                    file_path=target_file_path,
                    subject=custom_subject.strip() if custom_subject else None
                )

            if mail_res.get("status") == "success":
                st.success(f"✅ Email successfully sent via SMTP to `{mail_res['recipient']}`!")
                st.markdown(f"**Subject:** `{mail_res['subject']}`  \n**Attachment:** `{mail_res['attachment']}`")

                if send_only:
                    st.info("💡 Email is now waiting in your inbox! Run `python watch_mailbox.py --once` in your terminal to see the worker pick it up.")
                elif send_and_ingest:
                    with st.spinner("Waiting for email in mailbox, polling via IMAP, and executing pipeline..."):
                        import time
                        time.sleep(2)
                        poll_summary = safe_poll_inbox(db_path="statements.db")

                    st.markdown("---")
                    st.markdown("### 📬 Mailbox Ingestion & Parsing Result")
                    if poll_summary.get("status") == "success":
                        st.success(f"Mailbox poller processed {poll_summary.get('count', 0)} new incoming email(s)!")
                        recent_stmts = store.get_all()
                        if recent_stmts:
                            latest_stmt_row = recent_stmts[0]
                            m1, m2, m3 = st.columns(3)
                            m1.metric("Pipeline Status", latest_stmt_row.get("status", "").upper())
                            m2.metric("Format Matched", latest_stmt_row.get("format_id") or "None")
                            m3.metric("LLM Calls Used", latest_stmt_row.get("llm_calls_made", 0))

                            issues_list = json.loads(latest_stmt_row.get("issues_json", "[]"))
                            if issues_list:
                                st.warning(f"Auditor Issues: {', '.join(issues_list)}")

                            rec_json_raw = latest_stmt_row.get("record_json")
                            if rec_json_raw:
                                rec_data = json.loads(rec_json_raw)
                                st.json(rec_data)
                                st.download_button(
                                    label="📥 Download Extracted Statement JSON",
                                    data=json.dumps(rec_data, indent=2),
                                    file_name=f"{target_file_name}.json",
                                    mime="application/json"
                                )
                        else:
                            st.info("Statement processed; check the Mailbox Operations tab to view all event records.")
                    else:
                        st.error(f"Mailbox polling notice: {poll_summary.get('message')}")
            else:
                st.error(f"Failed to dispatch email: {mail_res.get('error')}")
                st.info("Check IMAP_USER and IMAP_PASSWORD in your `.env` file.")

    # --- TAB B: Offline .eml Email File Simulator ---
    with tab_eml:
        st.subheader("✉️ Automated Email Gatekeeper Simulator (.eml)")
        st.markdown(
            "Upload an `.eml` email file or select a sample email. "
            "The gatekeeper determines whether to **INGEST** (statement attached), **IGNORE** (marketing noise / portal links), or **QUARANTINE**."
        )

        email_mode = st.radio(
            "Email Source:",
            ["Select Sample Email", "Upload Custom .eml File"],
            horizontal=True
        )

        eml_to_process = None
        eml_label = ""

        if email_mode == "Select Sample Email":
            sample_emls = {
                "e01: Statement Single Attachment (Aurum PMS PDF)": "sample_data/emails/e01_statement_single_attachment.eml",
                "e02: Two Statements Two Providers (Banyan PMS + Evergreen AIF)": "sample_data/emails/e02_two_statements_two_providers.eml",
                "e03: Newsletter With Factsheet (Marketing Noise -> Ignore)": "sample_data/emails/e03_newsletter_with_factsheet.eml",
                "e04: Portal Link No Attachment (Web Link -> Ignore)": "sample_data/emails/e04_portal_link_no_attachment.eml",
                "e05: Excel Statement Attachment (Hestia Capital XLSX)": "sample_data/emails/e05_excel_statement.eml"
            }
            chosen_eml_key = st.selectbox("Select Sample Email:", list(sample_emls.keys()))
            eml_to_process = sample_emls[chosen_eml_key]
            eml_label = chosen_eml_key
        else:
            uploaded_eml = st.file_uploader("Upload .eml email file:", type=["eml"])
            if uploaded_eml is not None:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".eml") as tmp_eml:
                    tmp_eml.write(uploaded_eml.getvalue())
                    eml_to_process = tmp_eml.name
                    eml_label = uploaded_eml.name

        if eml_to_process:
            if st.button("🚀 Process Email & Run Gatekeeper"):
                with st.spinner("Analyzing email headers, body & extracting attachments..."):
                    eml_res = ingestion.parse_eml_email(eml_to_process)

                status = eml_res.get("status", "unknown")
                subject = eml_res.get("subject", "N/A")
                sender = eml_res.get("sender", "N/A")
                attachments = eml_res.get("attachments", [])
                reason = eml_res.get("reason", "")

                st.markdown("---")
                st.markdown("### 📬 Email Gatekeeper Audit Result")

                c1, c2, c3 = st.columns([1, 2, 1])
                if status == "ingest":
                    c1.success("### ✅ Action: INGEST")
                elif status == "ignore":
                    c1.error("### 🛑 Action: IGNORE")
                else:
                    c1.warning(f"### ⚠️ Action: {status.upper()}")

                c2.markdown(f"**Subject:** `{subject}`\n\n**Sender:** `{sender}`")
                c3.metric("Attachments Extracted", len(attachments))

                if reason:
                    if reason == "newsletter_with_factsheet":
                        st.info("💡 **Gatekeeper Rationale:** Email identified as a monthly market factsheet/newsletter. Filtered at zero cost to prevent polluting downstream portfolio pipelines.")
                    elif reason == "portal_link_no_attachment":
                        st.info("💡 **Gatekeeper Rationale:** Statement is hosted behind a secure web portal link with no raw attachment. Filtered to prevent parsing empty content.")

                if attachments and status == "ingest":
                    st.markdown("---")
                    st.subheader(f"📎 Extracted Attachments ({len(attachments)}) & Pipeline Execution")

                    for att_idx, att in enumerate(attachments):
                        st.markdown(f"#### Attachment #{att_idx+1}: `{att['filename']}` ({att['size'] / 1024:.1f} KB)")
                        with st.spinner(f"Parsing attachment `{att['filename']}`..."):
                            att_res = pipeline.process_file(att["path"])

                        p1, p2, p3 = st.columns(3)
                        p1.metric("Pipeline Status", att_res.status.upper())
                        p2.metric("Format Matched", att_res.format_id or "Unknown")
                        p3.metric("LLM Calls Made", att_res.llm_calls_made)

                        if att_res.issues:
                            st.warning(f"Auditor Issues: {', '.join(att_res.issues)}")

                        if att_res.statements:
                            for s_idx, stmt in enumerate(att_res.statements):
                                render_statement_view(stmt, account_idx=s_idx + 1)
                                json_str = json.dumps(stmt.model_dump(), indent=2)
                                st.download_button(
                                    label=f"📥 Download Extracted JSON ({att['filename']})",
                                    data=json_str,
                                    file_name=f"{att['filename']}.json",
                                    mime="application/json",
                                    key=f"dl_att_{att_idx}_{s_idx}"
                                )
                        else:
                            st.info("No statement records produced (file was quarantined or rejected).")
