"""Excel export for the captain dashboard — formatted as an interactive dispatch tool."""

from datetime import datetime, timezone
import psycopg2
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.formatting.rule import DataBarRule, ColorScaleRule
from openpyxl.utils import get_column_letter

from pems.config import DB_PARAMS
from pems.features import fetch_features
from pems.scoring import score_locations

def fetch_workflow_state(conn, run_tag):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                qt.task_id, qt.location_id, l.location_code, l.zone,
                qt.priority, qt.status, qt.task_reason, qt.created_at,
                e.employee_code AS assigned_to,
                qto.confirmed_cause, qto.resolution_notes, qto.resolved_at
            FROM quality_tasks qt
            JOIN locations l ON l.location_id = qt.location_id
            LEFT JOIN employees e ON e.employee_id = qt.assigned_shopper_id
            LEFT JOIN quality_task_outcomes qto ON qto.task_id = qt.task_id
            WHERE qt.location_id IN (
                SELECT DISTINCT pe.location_id
                FROM picking_events pe
                JOIN simulation_event_labels el ON el.event_id = pe.event_id
                WHERE el.run_tag = %s
            )
            ORDER BY CASE qt.priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, qt.task_id;
        """, (run_tag,))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

def fetch_zone_summary(conn, run_tag):
    """Fetch the data for the Pivot Table summary."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT l.zone, l.location_code, COUNT(DISTINCT pe.event_id) as anomalies
            FROM picking_events pe
            JOIN locations l ON l.location_id = pe.location_id
            JOIN simulation_event_labels el ON el.event_id = pe.event_id
            WHERE el.run_tag = %s AND el.is_anomaly = TRUE
            GROUP BY l.zone, l.location_code
            ORDER BY l.zone, anomalies DESC;
        """, (run_tag,))
        return cur.fetchall()

def fetch_employees(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT employee_code FROM employees WHERE employee_role = 'quality_shopper' ORDER BY employee_code;")
        return [r[0] for r in cur.fetchall()]

def write_excel(run_tag, output_stream):
    conn = psycopg2.connect(**DB_PARAMS)
    try:
        ranked = score_locations(fetch_features(run_tag, conn))
        workflow = fetch_workflow_state(conn, run_tag)
        zone_data = fetch_zone_summary(conn, run_tag)
        shoppers = fetch_employees(conn)
    finally:
        conn.close()

    wb = Workbook()
    
    # ==========================================
    # SHEET 1: DISPATCH SHEET (Interactive)
    # ==========================================
    ws = wb.active
    ws.title = "Dispatch Sheet"

    font_title = Font(bold=True, size=16, color="1F4E78")
    font_header = Font(bold=True, color="FFFFFF")
    font_bold = Font(bold=True)
    
    fill_dark_blue = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    thin_border = Border(left=Side(style='thin'), right=Side(style='thin'), 
                         top=Side(style='thin'), bottom=Side(style='thin'))

    # Header
    ws["A1"] = "PEMS Daily Dispatch & Triage"
    ws["A1"].font = font_title
    ws["A2"] = f"Run: {run_tag} | Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    ws["A2"].font = font_bold

    # --- Data Table Headers ---
    headers = [
        "Priority", "Location", "Zone", "Risk Score", "Anomalies", 
        "Pickers", "Assigned Shopper", "Shopper Outcome", "Shopper Notes"
    ]
    start_row = 5
    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=start_row, column=col_idx, value=header)
        cell.font = font_header
        cell.fill = fill_dark_blue
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    # Map tasks
    tasks_by_loc = {t["location_id"]: t for t in workflow}
    visible = [r for r in ranked if r["anomaly_events"] > 0]
    
    row_idx = start_row + 1
    for r in visible:
        loc_id = r["location_id"]
        task = tasks_by_loc.get(loc_id)
        priority = ("high" if r["proposed_score"] >= 0.7 else "medium" if r["proposed_score"] >= 0.4 else "low")
        
        # Determine Zone based on location string (e.g. SIM-LOC-A-12 -> Zone A)
        zone = task["zone"] if task else "?"

        row_values = [
            priority.upper(), loc_id, zone,
            round(r["proposed_score"], 3), 
            r["anomaly_events"], r["distinct_pickers"],
            task["assigned_to"] if task and task["assigned_to"] else "",
            task["confirmed_cause"] if task and task["confirmed_cause"] else "",
            task["resolution_notes"] if task and task["resolution_notes"] else "",
        ]

        for col_idx, val in enumerate(row_values, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = thin_border
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        row_idx += 1

    # --- Data Bars & Color Scales (The "Excel" feel) ---
    if row_idx > start_row + 1:
        # Risk Score: Gradient color scale (Red to Green)
        rule_score = ColorScaleRule(start_type='num', start_value=0, start_color='63BE7B',
                                    mid_type='num', mid_value=0.5, mid_color='FFEB84',
                                    end_type='num', end_value=1, end_color='F8696B')
        ws.conditional_formatting.add(f"D{start_row+1}:D{row_idx-1}", rule_score)
        
        # Anomalies: Blue Data Bar
        max_anomalies = max([r["anomaly_events"] for r in visible])
        rule_anom = DataBarRule(start_type='num', start_value=0, end_type='num', end_value=max_anomalies, color="5B9BD5")
        ws.conditional_formatting.add(f"E{start_row+1}:E{row_idx-1}", rule_anom)

    # --- Data Validation (Dropdowns) ---
    # 1. Shopper Names (Column G)
    dv_shopper = DataValidation(type="list", formula1=f'"{",".join(shoppers)}"', allow_blank=True, showDropDown=False)
    ws.add_data_validation(dv_shopper)
    dv_shopper.add(f"G{start_row+1}:G{row_idx-1}")

    # 2. Outcomes (Column H)
    dv_outcome = DataValidation(
        type="list", 
        formula1='"incorrect_product_placement,damaged_barcode,mixed_products,missing_stock,no_problem_found,other"',
        allow_blank=True, showDropDown=False)
    ws.add_data_validation(dv_outcome)
    dv_outcome.add(f"H{start_row+1}:H{row_idx-1}")

    # Formatting
    widths = [10, 10, 8, 12, 12, 10, 18, 26, 40]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # ==========================================
    # SHEET 2: ZONE SUMMARY (Pivot-Style)
    # ==========================================
    ws2 = wb.create_sheet("Zone Summary")
    ws2["A1"] = "Anomalies by Zone"
    ws2["A1"].font = font_title

    ws2["A3"] = "Zone"
    ws2["B3"] = "Total Anomalies"
    for cell in ws2[3]:
        if cell.value:
            cell.font = font_header
            cell.fill = fill_dark_blue

    # Aggregate zones in Python
    zone_counts = {}
    for zone, loc, count in zone_data:
        zone_counts[zone] = zone_counts.get(zone, 0) + count

    r_idx = 4
    for zone, count in sorted(zone_counts.items()):
        ws2.cell(row=r_idx, column=1, value=f"Zone {zone}").border = thin_border
        ws2.cell(row=r_idx, column=2, value=count).border = thin_border
        r_idx += 1
        
    ws2.column_dimensions['A'].width = 15
    ws2.column_dimensions['B'].width = 18

    # Add data bars to summary
    if r_idx > 4:
        max_count = max(zone_counts.values())
        rule_zone = DataBarRule(start_type='num', start_value=0, end_type='num', end_value=max_count, color="FF5733")
        ws2.conditional_formatting.add(f"B4:B{r_idx-1}", rule_zone)

    # Print Setup
    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    wb.save(output_stream)
    return output_stream