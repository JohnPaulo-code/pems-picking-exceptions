"""Generates a styled training and rollout PowerPoint deck for warehouse captains."""
import sys
from datetime import datetime, timezone
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN

from pems.config import DB_PARAMS
import psycopg2
from pems.features import fetch_features
from pems.scoring import score_locations

# --- Theme Colors ---
BG_COLOR = RGBColor(20, 24, 30)       
TEXT_WHITE = RGBColor(255, 255, 255)
ACCENT_GREEN = RGBColor(46, 204, 113) 
ACCENT_RED = RGBColor(231, 76, 60)    
ACCENT_YELLOW = RGBColor(241, 196, 15)

def style_slide(slide):
    background = slide.background
    fill = background.fill
    fill.solid()
    fill.fore_color.rgb = BG_COLOR
    
    title = slide.shapes.title
    title.text_frame.paragraphs[0].font.color.rgb = TEXT_WHITE
    title.text_frame.paragraphs[0].font.bold = True
    title.text_frame.paragraphs[0].font.size = Pt(36)

def style_text_frame(tf, default_color=TEXT_WHITE):
    for p in tf.paragraphs:
        for run in p.runs:
            run.font.color.rgb = default_color

def add_title_slide(prs, run_tag):
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    style_slide(slide)
    
    title = slide.shapes.title
    subtitle = slide.placeholders[1]
    
    title.text = "PEMS"
    title.text_frame.paragraphs[0].font.size = Pt(72)
    
    subtitle.text = f"Picking Exception Management System\nCaptain Rollout & Training\n\nRun: {run_tag}  |  {datetime.now(timezone.utc).strftime('%B %d, %Y')}"
    style_text_frame(subtitle.text_frame)

def add_problem_slide(prs):
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    style_slide(slide)
    slide.shapes.title.text = "Why are we doing this?"
    
    tf = slide.placeholders[1].text_frame
    tf.text = "The Look-alike Problem"
    tf.paragraphs[0].font.bold = True
    tf.paragraphs[0].font.size = Pt(28)
    tf.paragraphs[0].font.color.rgb = ACCENT_RED
    
    p = tf.add_paragraph()
    p.text = "• When two similar products are on the same shelf, pickers scan the wrong one."
    p.font.size = Pt(20)
    p.space_after = Pt(12)
    
    p = tf.add_paragraph()
    p.text = "• The scanner rejects it, but it looks like a barcode glitch. The picker overrides it."
    p.font.size = Pt(20)
    p.space_after = Pt(12)

    p = tf.add_paragraph()
    p.text = "• The wrong item ships. We find out days later when a customer complains."
    p.font.size = Pt(20)
    p.space_after = Pt(24)

    p = tf.add_paragraph()
    p.text = "Goal: Catch the pattern before the customer does."
    p.font.size = Pt(24)
    p.font.bold = True
    p.font.color.rgb = ACCENT_YELLOW
    style_text_frame(tf)

def add_solution_slide(prs):
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    style_slide(slide)
    slide.shapes.title.text = "How the system helps"
    
    tf = slide.placeholders[1].text_frame
    tf.text = "We are not watching individual pickers."
    tf.paragraphs[0].font.bold = True
    tf.paragraphs[0].font.size = Pt(24)
    tf.paragraphs[0].font.color.rgb = ACCENT_GREEN
    
    p = tf.add_paragraph()
    p.text = "We are looking at the shelf. If multiple pickers scan the same wrong product at the same location, it's a shelf problem."
    p.font.size = Pt(20)
    p.space_after = Pt(24)

    p = tf.add_paragraph()
    p.text = "Every morning, the system generates a Dispatch Sheet."
    p.font.bold = True
    p.font.size = Pt(22)
    
    p = tf.add_paragraph()
    p.text = "It ranks locations by Risk Score, based on:"
    p.font.size = Pt(20)
    
    p = tf.add_paragraph()
    p.text = "1. Frequency\n2. Diversity\n3. Consistency\n4. Recency"
    p.font.size = Pt(20)
    p.level = 1
    style_text_frame(tf)

def add_demo_slide(prs):
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    style_slide(slide)
    slide.shapes.title.text = "The Captain Dashboard"
    
    tf = slide.placeholders[1].text_frame
    tf.text = "[ Insert screenshot of your Streamlit dashboard here ]"
    tf.paragraphs[0].font.size = Pt(24)
    tf.paragraphs[0].font.color.rgb = ACCENT_YELLOW
    tf.paragraphs[0].alignment = PP_ALIGN.CENTER
    
    p = tf.add_paragraph()
    p.text = "\nThis is your view. Open the browser, check the ranking, and see the evidence."
    p.font.size = Pt(20)
    p.alignment = PP_ALIGN.CENTER
    style_text_frame(tf)

def add_process_slide(prs):
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    style_slide(slide)
    slide.shapes.title.text = "Your Daily Workflow"
    
    tf = slide.placeholders[1].text_frame
    tf.text = "1. Review the suggested tasks."
    tf.paragraphs[0].font.size = Pt(22)
    tf.paragraphs[0].space_after = Pt(16)
    
    p = tf.add_paragraph()
    p.text = "2. Accept the ones that look right. Reject duplicates or known issues."
    p.font.size = Pt(22)
    p.space_after = Pt(16)

    p = tf.add_paragraph()
    p.text = "3. Assign a Quality Shopper."
    p.font.size = Pt(22)
    p.space_after = Pt(16)

    p = tf.add_paragraph()
    p.text = "4. Shopper visits, records the cause, and closes the loop."
    p.font.size = Pt(22)
    p.space_after = Pt(24)

    p = tf.add_paragraph()
    p.text = "The system learns from the shopper's feedback."
    p.font.size = Pt(24)
    p.font.bold = True
    p.font.color.rgb = ACCENT_GREEN
    style_text_frame(tf)

def add_results_slide(prs, run_tag, top_loc, top_anomalies):
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    style_slide(slide)
    slide.shapes.title.text = "System Status & Impact"
    
    tf = slide.placeholders[1].text_frame
    tf.text = f"Current Status (Run {run_tag}):"
    tf.paragraphs[0].font.bold = True
    tf.paragraphs[0].font.size = Pt(24)
    
    p = tf.add_paragraph()
    p.text = f"• Top flagged location: {top_loc}"
    p.font.size = Pt(22)
    
    p = tf.add_paragraph()
    p.text = f"• {top_anomalies} mismatch events detected"
    p.font.size = Pt(22)
    p.space_after = Pt(24)

    p = tf.add_paragraph()
    p.text = "This week, we have a clear list of where to send shoppers first."
    p.font.size = Pt(22)
    p.space_after = Pt(24)

    p = tf.add_paragraph()
    p.text = "Open the Excel dispatch sheet to manage your shift."
    p.font.bold = True
    p.font.size = Pt(26)
    p.font.color.rgb = ACCENT_GREEN
    style_text_frame(tf)

def build_deck(run_tag, output_file="PEMS_Captain_Training.pptx"):
    conn = psycopg2.connect(**DB_PARAMS)
    try:
        ranked = score_locations(fetch_features(run_tag, conn))
    finally:
        conn.close()

    visible = [r for r in ranked if r["anomaly_events"] > 0]
    top_loc = visible[0]["location_id"] if visible else "N/A"
    top_anomalies = visible[0]["anomaly_events"] if visible else 0

    prs = Presentation()
    add_title_slide(prs, run_tag)
    add_problem_slide(prs)
    add_solution_slide(prs)
    add_demo_slide(prs)
    add_process_slide(prs)
    add_results_slide(prs, run_tag, top_loc, top_anomalies)

    prs.save(output_file)
    print(f"Styled deck saved to {output_file}")

if __name__ == "__main__":
    tag = sys.argv[1] if len(sys.argv) > 1 else "SIM003"
    build_deck(tag)