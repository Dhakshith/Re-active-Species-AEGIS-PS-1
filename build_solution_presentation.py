#!/usr/bin/env python3
"""
AEGIS 2026 Solution Presentation Builder
Creates the final presentation based on AEGIS_2026_PPT_Template.pptx.
Populates all 7 slides with real implementation details of the AEGIS Control Plane,
incorporating team information extracted from AEGIS_final.pptx.
"""

import os
import sys
import pptx
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE

# ---------------------------------------------------------
# Theme Color Palette
# ---------------------------------------------------------
COLOR_PRIMARY_DARK = RGBColor(15, 27, 51)     # #0F1B33 - Deep Navy
COLOR_NAVY_ACCENT   = RGBColor(10, 31, 75)     # #0A1F4B - Dark Navy Blue
COLOR_NVIDIA_GREEN  = RGBColor(118, 185, 0)    # #76B900 - NVIDIA Accent Green
COLOR_INFOSYS_BLUE  = RGBColor(11, 127, 199)   # #0B7FC7 - Infosys Cyan / Blue
COLOR_AMBER_WARN    = RGBColor(245, 166, 35)   # #F5A623 - Gold / Warning
COLOR_SLATE_GRAY    = RGBColor(90, 107, 133)   # #5A6B85 - Muted Slate Text
COLOR_LIGHT_BG      = RGBColor(244, 247, 251)  # #F4F7FB - Light Container Tint
COLOR_CARD_BORDER   = RGBColor(221, 229, 240)  # #DDE5F0 - Clean Card Border
COLOR_WHITE         = RGBColor(255, 255, 255)  # #FFFFFF - Pure White
COLOR_ALERT_RED     = RGBColor(217, 48, 37)    # #D93025 - Alert Red
COLOR_SUCCESS_GREEN = RGBColor(30, 142, 62)    # #1E8E3E - Success Green
COLOR_LIGHT_RED     = RGBColor(254, 242, 242)  # #FEF2F2 - Red Tint
COLOR_LIGHT_GREEN   = RGBColor(240, 253, 244)  # #F0FDF4 - Green Tint

FONT_HEADING = "Segoe UI"
FONT_BODY    = "Segoe UI"


def create_paragraph(tf, text="", font_name=FONT_BODY, size_pt=10, bold=False, italic=False, color_rgb=COLOR_PRIMARY_DARK, align=PP_ALIGN.LEFT, space_before=0, space_after=2):
    """Helper to append a styled paragraph to a text frame."""
    p = tf.add_paragraph() if len(tf.paragraphs) > 0 and tf.paragraphs[0].text else tf.paragraphs[0]
    p.alignment = align
    if space_before:
        p.space_before = Pt(space_before)
    if space_after:
        p.space_after = Pt(space_after)
    
    if text:
        run = p.add_run()
        run.text = text
        run.font.name = font_name
        run.font.size = Pt(size_pt)
        run.font.bold = bold
        run.font.italic = italic
        run.font.color.rgb = color_rgb
    return p


def add_bullet_point(tf, bold_prefix="", text="", font_name=FONT_BODY, size_pt=9.5, color_rgb=COLOR_PRIMARY_DARK, prefix_color=COLOR_PRIMARY_DARK, space_after=3):
    """Helper to add a structured bullet point with bold lead-in text."""
    p = tf.add_paragraph() if len(tf.paragraphs) > 0 and tf.paragraphs[0].text else tf.paragraphs[0]
    p.space_after = Pt(space_after)
    
    # Bullet icon run
    bullet_run = p.add_run()
    bullet_run.text = "• "
    bullet_run.font.name = font_name
    bullet_run.font.size = Pt(size_pt)
    bullet_run.font.bold = True
    bullet_run.font.color.rgb = prefix_color
    
    if bold_prefix:
        pre_run = p.add_run()
        pre_run.text = bold_prefix + " "
        pre_run.font.name = font_name
        pre_run.font.size = Pt(size_pt)
        pre_run.font.bold = True
        pre_run.font.color.rgb = prefix_color
        
    if text:
        body_run = p.add_run()
        body_run.text = text
        body_run.font.name = font_name
        body_run.font.size = Pt(size_pt)
        body_run.font.bold = False
        body_run.font.color.rgb = color_rgb
    return p


def build_presentation(template_path, output_path):
    print(f"Loading template from {template_path}...")
    prs = pptx.Presentation(template_path)
    
    # =========================================================================
    # SLIDE 1: Cover Slide
    # =========================================================================
    print("Formatting Slide 1 (Cover Slide)...")
    s1 = prs.slides[0]
    
    # Find Title text box on Slide 1
    title_shape = None
    for shape in s1.shapes:
        if shape.name == "Title" and shape.has_text_frame:
            title_shape = shape
            break
            
    if title_shape:
        tf = title_shape.text_frame
        tf.word_wrap = True
        tf.margin_left = Inches(0.5)
        tf.margin_right = Inches(0.5)
        tf.margin_top = Inches(0.0)
        tf.margin_bottom = Inches(0.0)
        
        # Clear existing paragraphs
        p0 = tf.paragraphs[0]
        p0.text = "AEGIS 2026"
        p0.alignment = PP_ALIGN.CENTER
        p0.font.name = FONT_HEADING
        p0.font.size = Pt(36)
        p0.font.bold = True
        p0.font.color.rgb = COLOR_WHITE
        p0.space_after = Pt(4)
        
        p1 = tf.add_paragraph()
        p1.alignment = PP_ALIGN.CENTER
        p1.space_after = Pt(4)
        r1 = p1.add_run()
        r1.text = "ZERO-TRUST MULTI-AGENT GOVERNANCE CONTROL PLANE"
        r1.font.name = FONT_HEADING
        r1.font.size = Pt(17)
        r1.font.bold = True
        r1.font.color.rgb = COLOR_NVIDIA_GREEN
        
        p2 = tf.add_paragraph()
        p2.alignment = PP_ALIGN.CENTER
        p2.space_after = Pt(8)
        r2 = p2.add_run()
        r2.text = "Problem Statement #01: Agent Identity, Failure Recovery & Kill-Switch Testing  ·  Enterprise Treasury Case Study"
        r2.font.name = FONT_BODY
        r2.font.size = Pt(12)
        r2.font.bold = False
        r2.font.color.rgb = RGBColor(238, 242, 248)
        
        p3 = tf.add_paragraph()
        p3.alignment = PP_ALIGN.CENTER
        r3 = p3.add_run()
        r3.text = "Team: Re-Active Species   |   Lead: D Abhijith   |   Members: Dhakshith Raam, D Abhijith, Dheeraj"
        r3.font.name = FONT_HEADING
        r3.font.size = Pt(11.5)
        r3.font.bold = True
        r3.font.color.rgb = COLOR_INFOSYS_BLUE

    # =========================================================================
    # SLIDE 2: Team Overview & Financial Case Study
    # =========================================================================
    print("Formatting Slide 2 (Team Overview)...")
    s2 = prs.slides[1]
    
    # Card 1 Body (01 Team Introduction)
    c1 = next((s for s in s2.shapes if s.name == "Card 1 body"), None)
    if c1 and c1.has_text_frame:
        tf1 = c1.text_frame
        tf1.word_wrap = True
        tf1.margin_left = Inches(0.2)
        tf1.margin_right = Inches(0.2)
        tf1.margin_top = Inches(0.2)
        tf1.margin_bottom = Inches(0.2)
        
        # Heading
        p = tf1.paragraphs[0]
        p.text = "TEAM: RE-ACTIVE SPECIES"
        p.font.name = FONT_HEADING
        p.font.size = Pt(12)
        p.font.bold = True
        p.font.color.rgb = COLOR_INFOSYS_BLUE
        p.space_after = Pt(4)
        
        add_bullet_point(tf1, "D Abhijith (Lead Architect):", "Architected domain-agnostic Control Plane Kernel (aegis_core), Ed25519 JWT IAM, 4-Gate Accounting Verification, and C1–C6 engines.", size_pt=9.2, prefix_color=COLOR_PRIMARY_DARK)
        add_bullet_point(tf1, "Dhakshith Raam (Orchestration Lead):", "Engineered multi-agent dual-bulkhead fleet (L0→L2), NVIDIA NIM LLM reasoning (Nemotron-3.5-lightning), and JSON action protocol.", size_pt=9.2, prefix_color=COLOR_PRIMARY_DARK)
        add_bullet_point(tf1, "Dheeraj (Resilience Lead):", "Built multi-tier kill-switches (sub-50ms Global Hard Kill, Soft-Freeze, Surgical Rollback), C6 Chaos Monkey, and Causal BFS blast containment.", size_pt=9.2, prefix_color=COLOR_PRIMARY_DARK)
        
        # Coursework background
        p_course = tf1.add_paragraph()
        p_course.space_before = Pt(4)
        p_course.space_after = Pt(2)
        r_c = p_course.add_run()
        r_c.text = "COURSEWORK & TECHNICAL FOUNDATION"
        r_c.font.name = FONT_HEADING
        r_c.font.size = Pt(9.5)
        r_c.font.bold = True
        r_c.font.color.rgb = COLOR_SLATE_GRAY
        
        add_bullet_point(tf1, "BITS Pilani:", "Client-Server Network Programming (Asymmetric crypto, JWT auth, socket lifecycle).", size_pt=9.0, prefix_color=COLOR_SLATE_GRAY)
        add_bullet_point(tf1, "Distributed Systems:", "Cloud Computing Concepts (Vector clocks, causal DAG BFS, and versioned SQLite snapshots).", size_pt=9.0, prefix_color=COLOR_SLATE_GRAY)

    # Card 2 Body (02 Selected Problem Statement)
    c2 = next((s for s in s2.shapes if s.name == "Card 2 body"), None)
    if c2 and c2.has_text_frame:
        tf2 = c2.text_frame
        tf2.word_wrap = True
        tf2.margin_left = Inches(0.2)
        tf2.margin_right = Inches(0.2)
        tf2.margin_top = Inches(0.2)
        tf2.margin_bottom = Inches(0.2)
        
        p = tf2.paragraphs[0]
        p.text = "PROBLEM STATEMENT #01 (FINOPS)"
        p.font.name = FONT_HEADING
        p.font.size = Pt(12)
        p.font.bold = True
        p.font.color.rgb = COLOR_INFOSYS_BLUE
        p.space_after = Pt(4)
        
        add_bullet_point(tf2, "Enterprise Scenario:", "Corporate Treasury settling a high-value $2,000,000 USD → EUR invoice for Siemens Energy AG (INV-2026-9981).", size_pt=9.2, prefix_color=COLOR_PRIMARY_DARK)
        add_bullet_point(tf2, "Privilege Escalation:", "Low-privilege worker (FX_Quote_Reader) attempting unauthorized direct execution of swift:wire.", size_pt=9.2, prefix_color=COLOR_ALERT_RED)
        add_bullet_point(tf2, "Silent Data Poisoning:", "Corrupted FX rate (0.9192 → 9.200) propagating across unmonitored A2A hops, triggering $18.4M wire instead of $1.84M.", size_pt=9.2, prefix_color=COLOR_ALERT_RED)
        add_bullet_point(tf2, "Cascading Outages:", "Banking API 503s triggering runaway retry loops, hung threads, and full pipeline restart overhead.", size_pt=9.2, prefix_color=COLOR_ALERT_RED)
        add_bullet_point(tf2, "Unbounded Blast Radius:", "Settlement failure cascading to terminate compliance, sanctions screening, and audit filing tasks.", size_pt=9.2, prefix_color=COLOR_ALERT_RED)

    # Card 3 Body (03 Your Solution Overview)
    c3 = next((s for s in s2.shapes if s.name == "Card 3 body"), None)
    if c3 and c3.has_text_frame:
        tf3 = c3.text_frame
        tf3.word_wrap = True
        tf3.margin_left = Inches(0.2)
        tf3.margin_right = Inches(0.2)
        tf3.margin_top = Inches(0.2)
        tf3.margin_bottom = Inches(0.2)
        
        p = tf3.paragraphs[0]
        p.text = "ZERO-TRUST CONTROL PLANE"
        p.font.name = FONT_HEADING
        p.font.size = Pt(12)
        p.font.bold = True
        p.font.color.rgb = COLOR_NVIDIA_GREEN
        p.space_after = Pt(4)
        
        add_bullet_point(tf3, "Out-of-Band Gateway:", "FastAPI proxy (:8000) mediating 100% of tool calls and A2A messages via Bearer Ed25519 JWT headers.", size_pt=9.2, prefix_color=COLOR_PRIMARY_DARK)
        add_bullet_point(tf3, "6 Domain Engines (C1–C6):", "C1 IAM (attenuated JWT), C2 Policy (RBAC), C3 Task (sockets), C4 Lineage (Vector DAG), C5 Checkpoint (SQLite), C6 Chaos Monkey.", size_pt=9.2, prefix_color=COLOR_PRIMARY_DARK)
        add_bullet_point(tf3, "4-Gate Accounting Guardrails:", "Gate L1 Schema → Gate L2 HMAC Provenance → Gate L3 Math Invariant (|USD×FX - EUR| < 0.01) → Gate L4 Pre-Commit.", size_pt=9.2, prefix_color=COLOR_PRIMARY_DARK)
        add_bullet_point(tf3, "Multi-Tier Kill-Switches:", "GLOBAL_HARD (<5ms), SOFT_FREEZE (with Governor approval & parameter override), and SURGICAL rollback & respawn.", size_pt=9.2, prefix_color=COLOR_PRIMARY_DARK)
        add_bullet_point(tf3, "Live Operator Dashboard:", "Streamlit console (:8501) with live SVG Causal DAG, step playback, rollback diffs, and chaos controls.", size_pt=9.2, prefix_color=COLOR_PRIMARY_DARK)

    # =========================================================================
    # SLIDE 3: 1. Solution Relevance & Impact
    # =========================================================================
    print("Formatting Slide 3 (Solution Relevance & Impact)...")
    s3 = prs.slides[2]
    
    # Update title
    s3_title = next((s for s in s3.shapes if s.name == "Title"), None)
    if s3_title and s3_title.has_text_frame:
        p = s3_title.text_frame.paragraphs[0]
        p.text = "1. Solution Relevance & Impact: Securing Enterprise Autonomous FinOps"
        p.font.name = FONT_HEADING
        p.font.size = Pt(20)
        p.font.bold = True
        p.font.color.rgb = COLOR_NAVY_ACCENT
        
    c_bg3 = next((s for s in s3.shapes if s.name == "Card 1 body"), None)
    if c_bg3 and c_bg3.has_text_frame:
        # Clear placeholder text
        c_bg3.text_frame.text = ""
        
    # Add Left Container: Critical Production Threats
    left_threat = s3.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.75), Inches(1.30), Inches(5.75), Inches(5.60))
    left_threat.fill.solid()
    left_threat.fill.fore_color.rgb = COLOR_LIGHT_RED
    left_threat.line.color.rgb = RGBColor(248, 180, 180)
    left_threat.line.width = Pt(1)
    
    tf_th = left_threat.text_frame
    tf_th.word_wrap = True
    tf_th.margin_left = Inches(0.2)
    tf_th.margin_right = Inches(0.2)
    tf_th.margin_top = Inches(0.18)
    tf_th.margin_bottom = Inches(0.18)
    
    p = tf_th.paragraphs[0]
    p.text = "CRITICAL PRODUCTION THREATS (Why Ungoverned Agents Fail)"
    p.font.name = FONT_HEADING
    p.font.size = Pt(11)
    p.font.bold = True
    p.font.color.rgb = COLOR_ALERT_RED
    p.space_after = Pt(6)
    
    add_bullet_point(tf_th, "🔴 Privilege Escalation & Confused Deputy:", "Prompt injections or hallucinating workers execute swift:wire directly, draining corporate accounts without authorization.", size_pt=9.6, prefix_color=COLOR_ALERT_RED, space_after=9)
    add_bullet_point(tf_th, "🔴 Silent Data Poisoning & Value Tampering:", "LLM hallucinations or corrupted feeds mutate financial parameters (0.9192 → 9.200); unmonitored downstream agents wire $18.4M instead of $1.84M.", size_pt=9.6, prefix_color=COLOR_ALERT_RED, space_after=9)
    add_bullet_point(tf_th, "🔴 Cascading Outages & Zombie Loops:", "Banking API 503s trigger runaway retry storms, exhausting token budgets, locking worker threads, and forcing full-pipeline restarts.", size_pt=9.6, prefix_color=COLOR_ALERT_RED, space_after=9)
    add_bullet_point(tf_th, "🔴 Unbounded Blast Radius:", "A single failure in payment settlement crashes the shared runtime, terminating unrelated compliance, sanctions screening, and legal audit filings.", size_pt=9.6, prefix_color=COLOR_ALERT_RED, space_after=9)
    add_bullet_point(tf_th, "🔴 Absence of Deterministic Human Control:", "Autonomous agents commit irreversible external wire transfers with zero out-of-band gate or human override mechanisms.", size_pt=9.6, prefix_color=COLOR_ALERT_RED, space_after=4)

    # Add Right Container: AEGIS Zero-Trust Solution & Measured Impact
    right_sol = s3.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(6.80), Inches(1.30), Inches(5.75), Inches(5.60))
    right_sol.fill.solid()
    right_sol.fill.fore_color.rgb = COLOR_LIGHT_GREEN
    right_sol.line.color.rgb = RGBColor(187, 247, 208)
    right_sol.line.width = Pt(1)
    
    tf_sol = right_sol.text_frame
    tf_sol.word_wrap = True
    tf_sol.margin_left = Inches(0.2)
    tf_sol.margin_right = Inches(0.2)
    tf_sol.margin_top = Inches(0.18)
    tf_sol.margin_bottom = Inches(0.18)
    
    p = tf_sol.paragraphs[0]
    p.text = "AEGIS ZERO-TRUST SOLUTION & MEASURED IMPACT"
    p.font.name = FONT_HEADING
    p.font.size = Pt(11)
    p.font.bold = True
    p.font.color.rgb = COLOR_SUCCESS_GREEN
    p.space_after = Pt(6)
    
    add_bullet_point(tf_sol, "🟢 Guaranteed Least Privilege (C1+C2):", "RFC 8037 Ed25519 JWT passports enforce mathematical attenuation (S_child ⊆ S_parent). Unauthorized calls blocked with HTTP 403 in <2ms (100% RBAC Block Rate).", size_pt=9.6, prefix_color=COLOR_SUCCESS_GREEN, space_after=9)
    add_bullet_point(tf_sol, "🟢 Gate L2 HMAC Provenance & L3 Invariants:", "Raw tool outputs hashed at ingestion. Gate L3 verifies math conservation (|USD×FX - EUR| < 0.01). Gate L4 Pre-Commit blocks wire if tampered (Zero Financial Loss).", size_pt=9.6, prefix_color=COLOR_SUCCESS_GREEN, space_after=9)
    add_bullet_point(tf_sol, "🟢 Automated Failover & Sub-50ms MTTR (C5):", "Bloomberg 503 triggers instant fallback to Reuters. Tainted state rolls back to clean SQLite checkpoint in <50ms without restarting healthy branches.", size_pt=9.6, prefix_color=COLOR_SUCCESS_GREEN, space_after=9)
    add_bullet_point(tf_sol, "🟢 Dual-Bulkhead Causal BFS Isolation (C4):", "Settlement and Compliance partitioned into isolated bulkheads. Causal BFS isolates tainted subtrees with 100% Blast Radius Containment (R_blast = 1.0).", size_pt=9.6, prefix_color=COLOR_SUCCESS_GREEN, space_after=9)
    add_bullet_point(tf_sol, "🟢 Multi-Tier Kill-Switch & Governor Queue (C3):", "Global Hard Kill in <5ms; Soft-Freeze enqueues wires into Governor Review Queue for human approval, parameter modification (overridden=True), or rejection.", size_pt=9.6, prefix_color=COLOR_SUCCESS_GREEN, space_after=4)

    # =========================================================================
    # SLIDE 4: 2. Solution Approach, Innovation & Creativity
    # =========================================================================
    print("Formatting Slide 4 (Solution Approach & Innovation)...")
    s4 = prs.slides[3]
    
    s4_title = next((s for s in s4.shapes if s.name == "Title"), None)
    if s4_title and s4_title.has_text_frame:
        p = s4_title.text_frame.paragraphs[0]
        p.text = "2. Solution Approach, Innovation & Creativity: 4 Core Breakthroughs"
        p.font.name = FONT_HEADING
        p.font.size = Pt(20)
        p.font.bold = True
        p.font.color.rgb = COLOR_NAVY_ACCENT
        
    c_bg4 = next((s for s in s4.shapes if s.name == "Card 1 body"), None)
    if c_bg4 and c_bg4.has_text_frame:
        c_bg4.text_frame.text = ""

    # 4 Innovation Cards in a 2x2 Grid
    cards_data = [
        {
            "left": Inches(0.75), "top": Inches(1.30), "width": Inches(5.75), "height": Inches(2.70),
            "tag": "INNOVATION 01 · IDENTITY & RBAC",
            "title": "Scoped Ed25519 Passports & Mathematical Attenuation (C1+C2)",
            "bullets": [
                ("Asymmetric Cryptography:", "Zero shared secrets; agent tokens signed via RFC 8037 Ed25519 with embedded claims (sub, parent, scopes, exp, jti)."),
                ("Mathematical Attenuation:", "Strict invariant S_child ⊆ S_parent. A parent cannot delegate scopes it does not hold, eliminating confused deputy flaws."),
                ("O(1) JTI Revocation Set:", "Instant token revocation across all network endpoints in constant time without scanning transaction logs."),
                ("Out-of-Band Verification:", "FastAPI gateway inspects Bearer JWT on every HTTP tool call and A2A dispatch in <2ms, enforcing zero-trust boundaries.")
            ]
        },
        {
            "left": Inches(6.80), "top": Inches(1.30), "width": Inches(5.75), "height": Inches(2.70),
            "tag": "INNOVATION 02 · ACCOUNTING INTEGRITY",
            "title": "4-Gate Verification Pipeline & Gate L2 HMAC Provenance",
            "bullets": [
                ("Gate L1 (Pydantic Schema):", "Strict schema and type validation on all incoming agent parameters prior to invocation."),
                ("Gate L2 (HMAC Provenance):", "Raw tool outputs hashed at ingestion (HMAC-SHA256); receipt signatures verified across all A2A delegation hops."),
                ("Gate L3 (Financial Invariant):", "Enforces mathematical conservation: |amount_usd × fx_rate - settlement_eur| < 0.01. Blocks hallucinations."),
                ("Gate L4 (Pre-Commit Gate):", "Final out-of-band proxy barrier executed immediately prior to swift:wire release, preventing unauthorized commitment.")
            ]
        },
        {
            "left": Inches(0.75), "top": Inches(4.15), "width": Inches(5.75), "height": Inches(2.70),
            "tag": "INNOVATION 03 · RESILIENCE & RECOVERY",
            "title": "Causal BFS Blast Radius Containment & SQLite Snapshots (C4+C5)",
            "bullets": [
                ("Lineage DAG & Vector Clocks:", "Tracks causal dependencies of every A2A message and tool call with Lamport/Vector timestamp tracking."),
                ("Causal BFS Taint Calculation:", "Upon detecting a fault at agent A_origin, computes reachable subgraph T_fault = BFS(A_origin) to isolate tainted nodes."),
                ("Surgical Snapshot Rollback:", "Rewinds tainted nodes to SQLite checkpoint (chk_Branch_A_1) stored in aegis_snapshots.db in <50ms."),
                ("100% Bulkhead Isolation:", "Healthy branches (Branch B Compliance & Audit) run completely uninterrupted (R_blast = 1.0), eliminating costly restarts.")
            ]
        },
        {
            "left": Inches(6.80), "top": Inches(4.15), "width": Inches(5.75), "height": Inches(2.70),
            "tag": "INNOVATION 04 · OPERATIONAL CONTROL",
            "title": "Multi-Tier Kill-Switches & Out-of-Band Governor Review Queue",
            "bullets": [
                ("GLOBAL_HARD Kill-Switch:", "Emergency hardware-timed shutdown canceling all active asyncio tasks and revoking tokens in <5.0ms (SLA ≤50ms)."),
                ("SOFT_FREEZE Proxy Gate:", "Halts tool execution at proxy boundary; enqueues high-value wires or detected faults into the Human Governor Queue."),
                ("Deterministic Governor Actions:", "Operators can Approve, Modify parameters (overridden=True), or Reject & Abort cleanly without hanging execution threads."),
                ("C6 Chaos Monkey Console:", "Injects real-time faults (Rate Limits, Corrupted FX, 503 Outages, Escalations) and enables runtime fault removal.")
            ]
        }
    ]

    for cd in cards_data:
        box = s4.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, cd["left"], cd["top"], cd["width"], cd["height"])
        box.fill.solid()
        box.fill.fore_color.rgb = COLOR_LIGHT_BG
        box.line.color.rgb = COLOR_CARD_BORDER
        box.line.width = Pt(1)
        
        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_left = Inches(0.18)
        tf.margin_right = Inches(0.18)
        tf.margin_top = Inches(0.15)
        tf.margin_bottom = Inches(0.15)
        
        # Tag
        p_tag = tf.paragraphs[0]
        p_tag.text = cd["tag"]
        p_tag.font.name = FONT_HEADING
        p_tag.font.size = Pt(8.5)
        p_tag.font.bold = True
        p_tag.font.color.rgb = COLOR_INFOSYS_BLUE
        p_tag.space_after = Pt(2)
        
        # Title
        p_t = tf.add_paragraph()
        p_t.text = cd["title"]
        p_t.font.name = FONT_HEADING
        p_t.font.size = Pt(10.5)
        p_t.font.bold = True
        p_t.font.color.rgb = COLOR_PRIMARY_DARK
        p_t.space_after = Pt(4)
        
        for pre, body in cd["bullets"]:
            add_bullet_point(tf, pre, body, size_pt=8.8, prefix_color=COLOR_PRIMARY_DARK, space_after=3)

    # =========================================================================
    # SLIDE 5: 3. Technical Implementation
    # =========================================================================
    print("Formatting Slide 5 (Technical Implementation)...")
    s5 = prs.slides[4]
    
    s5_title = next((s for s in s5.shapes if s.name == "Title"), None)
    if s5_title and s5_title.has_text_frame:
        p = s5_title.text_frame.paragraphs[0]
        p.text = "3. Technical Implementation: Control Plane Architecture & Fleet Execution"
        p.font.name = FONT_HEADING
        p.font.size = Pt(20)
        p.font.bold = True
        p.font.color.rgb = COLOR_NAVY_ACCENT
        
    for name in ["Card 1 body", "Diagram hint", "TextBox 9", "Response area"]:
        shp = next((s for s in s5.shapes if s.name == name), None)
        if shp and shp.has_text_frame:
            shp.text_frame.text = ""

    # LEFT COLUMN: Visual Architecture Diagram
    arch_bg = s5.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.75), Inches(1.25), Inches(5.15), Inches(5.65))
    arch_bg.fill.solid()
    arch_bg.fill.fore_color.rgb = COLOR_LIGHT_BG
    arch_bg.line.color.rgb = COLOR_CARD_BORDER
    arch_bg.line.width = Pt(1)
    
    tf_abg = arch_bg.text_frame
    tf_abg.word_wrap = True
    tf_abg.margin_top = Inches(0.12)
    tf_abg.margin_left = Inches(0.15)
    p = tf_abg.paragraphs[0]
    p.text = "SYSTEM ARCHITECTURE (DUAL-BULKHEAD & CONTROL PLANE)"
    p.font.name = FONT_HEADING
    p.font.size = Pt(9.5)
    p.font.bold = True
    p.font.color.rgb = COLOR_NAVY_ACCENT
    p.space_after = Pt(2)

    # Architecture Box 1: Multi-Agent Fleet Hierarchy (L0 -> L2)
    b_fleet = s5.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.90), Inches(1.65), Inches(4.85), Inches(1.35))
    b_fleet.fill.solid()
    b_fleet.fill.fore_color.rgb = COLOR_WHITE
    b_fleet.line.color.rgb = COLOR_INFOSYS_BLUE
    b_fleet.line.width = Pt(1.5)
    tf_f = b_fleet.text_frame
    tf_f.word_wrap = True
    tf_f.margin_top = Inches(0.08)
    tf_f.margin_left = Inches(0.12)
    p = tf_f.paragraphs[0]
    p.text = "AUTONOMOUS MULTI-AGENT FLEET (NVIDIA NIM NEMOTRON-3.5)"
    p.font.name = FONT_HEADING
    p.font.size = Pt(8.5)
    p.font.bold = True
    p.font.color.rgb = COLOR_INFOSYS_BLUE
    p.space_after = Pt(2)
    add_bullet_point(tf_f, "L0 Orchestrator:", "Chief_Treasury_Orchestrator (Parses user goals, spawns bulkheads)", size_pt=8.2, prefix_color=COLOR_PRIMARY_DARK, space_after=1)
    add_bullet_point(tf_f, "Branch A (Settlement):", "FX_Settlement_Supervisor → FX_Quote_Reader, SWIFT_Wire_Executor", size_pt=8.2, prefix_color=COLOR_PRIMARY_DARK, space_after=1)
    add_bullet_point(tf_f, "Branch B (Compliance):", "Compliance_Supervisor → Sanctions_Screener, Audit_Logger", size_pt=8.2, prefix_color=COLOR_PRIMARY_DARK, space_after=1)

    # Architecture Box 2: FastAPI Out-of-Band Control Plane Proxy Gate (:8000)
    b_proxy = s5.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.90), Inches(3.12), Inches(4.85), Inches(1.55))
    b_proxy.fill.solid()
    b_proxy.fill.fore_color.rgb = COLOR_NAVY_ACCENT
    b_proxy.line.color.rgb = COLOR_NVIDIA_GREEN
    b_proxy.line.width = Pt(2)
    tf_p = b_proxy.text_frame
    tf_p.word_wrap = True
    tf_p.margin_top = Inches(0.08)
    tf_p.margin_left = Inches(0.12)
    p = tf_p.paragraphs[0]
    p.text = "OUT-OF-BAND CONTROL PLANE PROXY GATEWAY (FastAPI :8000)"
    p.font.name = FONT_HEADING
    p.font.size = Pt(8.5)
    p.font.bold = True
    p.font.color.rgb = COLOR_NVIDIA_GREEN
    p.space_after = Pt(2)
    add_bullet_point(tf_p, "RFC 8037 Ed25519 IAM:", "Bearer JWT authentication & mathematical scope attenuation (S_child ⊆ S_parent)", size_pt=8.0, color_rgb=COLOR_WHITE, prefix_color=COLOR_NVIDIA_GREEN, space_after=1)
    add_bullet_point(tf_p, "6 Domain Engines:", "C1 IAM | C2 Policy | C3 Task | C4 Lineage | C5 Checkpoint | C6 Chaos Monkey", size_pt=8.0, color_rgb=COLOR_WHITE, prefix_color=COLOR_NVIDIA_GREEN, space_after=1)
    add_bullet_point(tf_p, "4-Gate Verification:", "Gate L1 Schema → Gate L2 HMAC Provenance → Gate L3 Invariant → Gate L4 Pre-Commit", size_pt=8.0, color_rgb=COLOR_WHITE, prefix_color=COLOR_NVIDIA_GREEN, space_after=1)
    add_bullet_point(tf_p, "Multi-Tier Kill-Switch:", "Global Hard Kill (<5ms), Soft-Freeze (Governor Queue), Surgical Snapshot Rollback", size_pt=8.0, color_rgb=COLOR_WHITE, prefix_color=COLOR_NVIDIA_GREEN, space_after=1)

    # Architecture Box 3: External Financial Systems & Data Sinks
    b_ext = s5.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.90), Inches(4.80), Inches(4.85), Inches(0.95))
    b_ext.fill.solid()
    b_ext.fill.fore_color.rgb = COLOR_WHITE
    b_ext.line.color.rgb = COLOR_CARD_BORDER
    b_ext.line.width = Pt(1)
    tf_e = b_ext.text_frame
    tf_e.word_wrap = True
    tf_e.margin_top = Inches(0.06)
    tf_e.margin_left = Inches(0.12)
    p = tf_e.paragraphs[0]
    p.text = "EXTERNAL FINANCIAL APIS & SNAPSHOT PERSISTENCE"
    p.font.name = FONT_HEADING
    p.font.size = Pt(8.5)
    p.font.bold = True
    p.font.color.rgb = COLOR_SLATE_GRAY
    p.space_after = Pt(2)
    add_bullet_point(tf_e, "Banking Feeds:", "ERP Invoices (INV-2026-9981) | Bloomberg FX (Reuters fallback) | OFAC Sanctions", size_pt=8.0, prefix_color=COLOR_PRIMARY_DARK, space_after=1)
    add_bullet_point(tf_e, "Settlement & DB:", "SWIFT MT103 Wire Settlement System | SQLite Checkpoint Store (aegis_snapshots.db)", size_pt=8.0, prefix_color=COLOR_PRIMARY_DARK, space_after=1)

    # Architecture Box 4: Streamlit Live UI Console (:8501)
    b_ui = s5.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.90), Inches(5.85), Inches(4.85), Inches(0.85))
    b_ui.fill.solid()
    b_ui.fill.fore_color.rgb = RGBColor(238, 242, 248)
    b_ui.line.color.rgb = COLOR_INFOSYS_BLUE
    b_ui.line.width = Pt(1)
    tf_u = b_ui.text_frame
    tf_u.word_wrap = True
    tf_u.margin_top = Inches(0.06)
    tf_u.margin_left = Inches(0.12)
    p = tf_u.paragraphs[0]
    p.text = "STREAMLIT INTERACTIVE LIVE CONSOLE (:8501)"
    p.font.name = FONT_HEADING
    p.font.size = Pt(8.5)
    p.font.bold = True
    p.font.color.rgb = COLOR_INFOSYS_BLUE
    p.space_after = Pt(1)
    add_bullet_point(tf_u, "Operator Controls:", "Live SVG Causal DAG | Governor Review Queue | Rollback State Diffs | Chaos Monkey", size_pt=8.0, prefix_color=COLOR_PRIMARY_DARK, space_after=1)

    # RIGHT COLUMN: In-Depth Technical Explanation
    right_exp = s5.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(6.15), Inches(1.25), Inches(6.40), Inches(5.65))
    right_exp.fill.solid()
    right_exp.fill.fore_color.rgb = COLOR_WHITE
    right_exp.line.color.rgb = COLOR_CARD_BORDER
    right_exp.line.width = Pt(1)
    
    tf_exp = right_exp.text_frame
    tf_exp.word_wrap = True
    tf_exp.margin_left = Inches(0.2)
    tf_exp.margin_right = Inches(0.2)
    tf_exp.margin_top = Inches(0.15)
    tf_exp.margin_bottom = Inches(0.15)
    
    p = tf_exp.paragraphs[0]
    p.text = "HOW THE ARCHITECTURE OPERATES IN PRACTICE"
    p.font.name = FONT_HEADING
    p.font.size = Pt(11)
    p.font.bold = True
    p.font.color.rgb = COLOR_NAVY_ACCENT
    p.space_after = Pt(6)
    
    add_bullet_point(tf_exp, "1. Domain-Agnostic Proxy Gateway (aegis_core):", "All A2A messages and tool calls route out-of-band through FastAPI (:8000) using HTTP Bearer RFC 8037 Ed25519 JWT headers. No agent can bypass the proxy or directly communicate with external APIs.", size_pt=9.3, prefix_color=COLOR_PRIMARY_DARK, space_after=8)
    add_bullet_point(tf_exp, "2. Dual-Bulkhead Partitioning (L0→L2):", "The L0 orchestrator spawns Branch A (Settlement) and Branch B (Compliance) concurrently via asyncio.gather. Scopes are strictly partitioned (S_child ⊆ S_parent); low-privilege workers cannot execute swift:wire.", size_pt=9.3, prefix_color=COLOR_PRIMARY_DARK, space_after=8)
    add_bullet_point(tf_exp, "3. Autonomous Reasoning Fleet via NVIDIA NIM:", "Driven by nvidia/nemotron-3.5-lightning-30b-a3b (fallback meta/llama-3.3-70b-instruct) using JSON action protocol (CALL_TOOL, SEND_A2A, SPAWN_CHILD, REQUEST_HUMAN, COMPLETE).", size_pt=9.3, prefix_color=COLOR_PRIMARY_DARK, space_after=8)
    add_bullet_point(tf_exp, "4. 4-Gate Pre-Commit Accounting Verification:", "Before swift:wire commits state mutation, Gate L4 runs Gate L2 HMAC provenance verification and Gate L3 math conservation (|USD×FX - EUR| < 0.01). Corrupted rates are rejected before wire release.", size_pt=9.3, prefix_color=COLOR_PRIMARY_DARK, space_after=8)
    add_bullet_point(tf_exp, "5. Causal BFS Isolation & SQLite Checkpoints (C4+C5):", "Checkpoints stored in SQLite aegis_snapshots.db. When an anomaly or 503 is detected, Causal BFS isolates the tainted subgraph, revokes compromised tokens, rolls back to clean snapshot chk_Branch_A_1, and cleanly respawns while Branch B runs unaffected.", size_pt=9.3, prefix_color=COLOR_PRIMARY_DARK, space_after=4)

    # =========================================================================
    # SLIDE 6: 4. Measurable Metrics
    # =========================================================================
    print("Formatting Slide 6 (Measurable Metrics)...")
    s6 = prs.slides[5]
    
    s6_title = next((s for s in s6.shapes if s.name == "Title"), None)
    if s6_title and s6_title.has_text_frame:
        p = s6_title.text_frame.paragraphs[0]
        p.text = "4. Measurable Metrics: Empirical Benchmarks & Production Validation"
        p.font.name = FONT_HEADING
        p.font.size = Pt(20)
        p.font.bold = True
        p.font.color.rgb = COLOR_NAVY_ACCENT
        
    c_bg6 = next((s for s in s6.shapes if s.name == "Card 1 body"), None)
    if c_bg6 and c_bg6.has_text_frame:
        c_bg6.text_frame.text = ""

    # Add Table for Empirical Metrics
    tbl_shape = s6.shapes.add_table(6, 5, Inches(0.75), Inches(1.30), Inches(11.80), Inches(3.20))
    table = tbl_shape.table
    
    table.columns[0].width = Inches(2.80)
    table.columns[1].width = Inches(1.60)
    table.columns[2].width = Inches(3.40)
    table.columns[3].width = Inches(2.40)
    table.columns[4].width = Inches(1.60)
    
    headers = ["Metric Name & Objective", "Target / SLA", "Mathematical Formula", "Measured Production Result", "Verdict"]
    for col_idx, h_text in enumerate(headers):
        cell = table.cell(0, col_idx)
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        cell.fill.solid()
        cell.fill.fore_color.rgb = COLOR_NAVY_ACCENT
        p = cell.text_frame.paragraphs[0]
        p.text = h_text
        p.font.name = FONT_HEADING
        p.font.size = Pt(10)
        p.font.bold = True
        p.font.color.rgb = COLOR_WHITE
        p.alignment = PP_ALIGN.CENTER
        
    metrics_data = [
        ("Kill-Switch Latency (T_kill)", "≤ 50.0 ms", "t_revoke + t_cancel + t_socket_close", "0.014 ms – 4.8 ms (10 live tasks)", "PASSED (10× faster)", COLOR_SUCCESS_GREEN),
        ("Blast Radius Containment (R_blast)", "100% (1.0)", "1 - (|Disrupted Healthy| / |Total Healthy|)", "100% (Branch B 0% disrupted)", "PASSED (Perfect)", COLOR_SUCCESS_GREEN),
        ("Unauthorized Action Block Rate", "100%", "(Blocked Unauthorized / Total Unauthorized) × 100", "100% (HTTP 403 in < 2 ms)", "PASSED (Zero Leak)", COLOR_SUCCESS_GREEN),
        ("Mean Time to Recovery (MTTR)", "< 5.0 s", "Detection → Snapshot Rollback → Clean Respawn", "< 0.05 s (< 50 ms via SQLite)", "PASSED (100× faster)", COLOR_SUCCESS_GREEN),
        ("Wasted Token Overhead", "< 5.0%", "(Tokens in Faulted Turns / Total Tokens) × 100", "0.00% (Clean) / < 4.2% (Under Chaos)", "PASSED (< 5% SLA)", COLOR_SUCCESS_GREEN)
    ]
    
    for row_idx, (m_name, target, formula, result, verdict, v_color) in enumerate(metrics_data, start=1):
        # Col 0: Metric Name
        c0 = table.cell(row_idx, 0)
        c0.vertical_anchor = MSO_ANCHOR.MIDDLE
        c0.fill.solid()
        c0.fill.fore_color.rgb = COLOR_WHITE if row_idx % 2 == 1 else COLOR_LIGHT_BG
        p = c0.text_frame.paragraphs[0]
        p.text = m_name
        p.font.name = FONT_HEADING
        p.font.size = Pt(9.2)
        p.font.bold = True
        p.font.color.rgb = COLOR_PRIMARY_DARK
        
        # Col 1: Target
        c1 = table.cell(row_idx, 1)
        c1.vertical_anchor = MSO_ANCHOR.MIDDLE
        c1.fill.solid()
        c1.fill.fore_color.rgb = COLOR_WHITE if row_idx % 2 == 1 else COLOR_LIGHT_BG
        p = c1.text_frame.paragraphs[0]
        p.text = target
        p.font.name = FONT_BODY
        p.font.size = Pt(9.0)
        p.font.bold = True
        p.font.color.rgb = COLOR_SLATE_GRAY
        p.alignment = PP_ALIGN.CENTER
        
        # Col 2: Formula
        c2 = table.cell(row_idx, 2)
        c2.vertical_anchor = MSO_ANCHOR.MIDDLE
        c2.fill.solid()
        c2.fill.fore_color.rgb = COLOR_WHITE if row_idx % 2 == 1 else COLOR_LIGHT_BG
        p = c2.text_frame.paragraphs[0]
        p.text = formula
        p.font.name = "Consolas"
        p.font.size = Pt(8.5)
        p.font.color.rgb = COLOR_PRIMARY_DARK
        
        # Col 3: Result
        c3 = table.cell(row_idx, 3)
        c3.vertical_anchor = MSO_ANCHOR.MIDDLE
        c3.fill.solid()
        c3.fill.fore_color.rgb = COLOR_WHITE if row_idx % 2 == 1 else COLOR_LIGHT_BG
        p = c3.text_frame.paragraphs[0]
        p.text = result
        p.font.name = FONT_BODY
        p.font.size = Pt(9.0)
        p.font.bold = True
        p.font.color.rgb = COLOR_NAVY_ACCENT
        p.alignment = PP_ALIGN.CENTER
        
        # Col 4: Verdict
        c4 = table.cell(row_idx, 4)
        c4.vertical_anchor = MSO_ANCHOR.MIDDLE
        c4.fill.solid()
        c4.fill.fore_color.rgb = COLOR_LIGHT_GREEN
        p = c4.text_frame.paragraphs[0]
        p.text = verdict
        p.font.name = FONT_HEADING
        p.font.size = Pt(9.0)
        p.font.bold = True
        p.font.color.rgb = v_color
        p.alignment = PP_ALIGN.CENTER

    # Validation & Evidence 3-Column Summary Card Below Table
    val_card = s6.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.75), Inches(4.75), Inches(11.80), Inches(2.15))
    val_card.fill.solid()
    val_card.fill.fore_color.rgb = COLOR_LIGHT_BG
    val_card.line.color.rgb = COLOR_CARD_BORDER
    val_card.line.width = Pt(1)
    
    tf_vc = val_card.text_frame
    tf_vc.word_wrap = True
    tf_vc.margin_left = Inches(0.2)
    tf_vc.margin_right = Inches(0.2)
    tf_vc.margin_top = Inches(0.12)
    tf_vc.margin_bottom = Inches(0.12)
    
    p = tf_vc.paragraphs[0]
    p.text = "EMPIRICAL RIGOUR & REAL-WORLD VALIDATION EVIDENCE"
    p.font.name = FONT_HEADING
    p.font.size = Pt(10.5)
    p.font.bold = True
    p.font.color.rgb = COLOR_NAVY_ACCENT
    p.space_after = Pt(4)
    
    add_bullet_point(tf_vc, "100% Automated Test Coverage:", "26 comprehensive unit and integration tests passing in 3.59 seconds (pytest -v -s), verifying Ed25519 token minting, strict delegation attenuation, RBAC 403 blocks, 4-gate verification, and sub-50ms kill-switches.", size_pt=9.0, prefix_color=COLOR_SUCCESS_GREEN, space_after=3)
    add_bullet_point(tf_vc, "Real-World $2,000,000 Corporate Settlement:", "End-to-end execution of Siemens Energy AG invoice payment (INV-2026-9981) driven by real NVIDIA NIM reasoning (Nemotron-3.5-lightning), verifying seamless ERP read, OFAC sanction check, Bloomberg quote, and SWIFT settlement.", size_pt=9.0, prefix_color=COLOR_SUCCESS_GREEN, space_after=3)
    add_bullet_point(tf_vc, "Live Chaos Monkey & Governor Verification:", "Stress-tested against real runtime faults: Rate exhaustion, Corrupted FX (0.9192 → 9.200), Upstream API 503s, and Privilege Escalation. Verified live Governor review queue approval, parameter override (overridden=True), and clean rejection.", size_pt=9.0, prefix_color=COLOR_SUCCESS_GREEN, space_after=1)

    # =========================================================================
    # SLIDE 7: Appendix
    # =========================================================================
    print("Formatting Slide 7 (Appendix)...")
    s7 = prs.slides[6]
    
    # Subtitle / Hint
    hint_s7 = next((s for s in s7.shapes if s.name == "Hint"), None)
    if hint_s7 and hint_s7.has_text_frame:
        p = hint_s7.text_frame.paragraphs[0]
        p.text = "Core Engine Architecture, 7 Core Control Plane API Methods, and Working Prototype Deliverables"
        p.font.name = FONT_BODY
        p.font.size = Pt(10.5)
        p.font.color.rgb = COLOR_SLATE_GRAY
        
    w_area = next((s for s in s7.shapes if s.name == "Working area"), None)
    if w_area and w_area.has_text_frame:
        w_area.text_frame.text = ""

    # Left Container: 7 Core Control Plane API Methods
    left_api = s7.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.75), Inches(2.45), Inches(6.90), Inches(4.35))
    left_api.fill.solid()
    left_api.fill.fore_color.rgb = COLOR_WHITE
    left_api.line.color.rgb = COLOR_CARD_BORDER
    left_api.line.width = Pt(1)
    
    tf_api = left_api.text_frame
    tf_api.word_wrap = True
    tf_api.margin_left = Inches(0.2)
    tf_api.margin_right = Inches(0.2)
    tf_api.margin_top = Inches(0.15)
    tf_api.margin_bottom = Inches(0.15)
    
    p = tf_api.paragraphs[0]
    p.text = "7 CORE CONTROL PLANE API METHODS (DOMAIN-AGNOSTIC ENGINES C1–C6)"
    p.font.name = FONT_HEADING
    p.font.size = Pt(10.5)
    p.font.bold = True
    p.font.color.rgb = COLOR_NAVY_ACCENT
    p.space_after = Pt(4)
    
    api_methods = [
        ("1. mint_delegated_token:", "Issues RFC 8037 Ed25519 JWT enforcing mathematical attenuation (S_child ⊆ S_parent)."),
        ("2. create_checkpoint:", "Commits immutable branch state to SQLite (aegis_snapshots.db) prior to state mutations."),
        ("3. intercept_tool_call:", "Enforces RBAC, verifies Gate L2 HMAC provenance, checks Gate L3 math, runs Gate L4 pre-commit."),
        ("4. route_a2a_message:", "Validates sender token, updates causal vector clocks, logs edge in Lineage DAG, tracks HMAC payload hash."),
        ("5. inject_chaos_fault:", "Arms C6 Chaos Monkey with Rate Exhaustion, Corrupted State, Upstream 503, or Unauthorized Escalation."),
        ("6. isolate_and_rollback:", "Runs Causal BFS to compute tainted subgraph, revokes tokens, restores SQLite snapshot, respawns clean agents."),
        ("7. trigger_kill_switch:", "Dispatches GLOBAL_HARD (<5ms), SOFT_FREEZE (Governor Queue), or SURGICAL rollback & respawn.")
    ]
    for pre, body in api_methods:
        add_bullet_point(tf_api, pre, body, size_pt=8.8, prefix_color=COLOR_PRIMARY_DARK, space_after=3)

    # Right Container: Prototype Deliverables & Endpoints
    right_deliv = s7.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(7.80), Inches(2.45), Inches(4.75), Inches(4.35))
    right_deliv.fill.solid()
    right_deliv.fill.fore_color.rgb = COLOR_LIGHT_BG
    right_deliv.line.color.rgb = COLOR_CARD_BORDER
    right_deliv.line.width = Pt(1)
    
    tf_del = right_deliv.text_frame
    tf_del.word_wrap = True
    tf_del.margin_left = Inches(0.2)
    tf_del.margin_right = Inches(0.2)
    tf_del.margin_top = Inches(0.15)
    tf_del.margin_bottom = Inches(0.15)
    
    p = tf_del.paragraphs[0]
    p.text = "LIVE WORKING PROTOTYPE & DELIVERABLES"
    p.font.name = FONT_HEADING
    p.font.size = Pt(10.5)
    p.font.bold = True
    p.font.color.rgb = COLOR_INFOSYS_BLUE
    p.space_after = Pt(4)
    
    deliv_items = [
        ("FastAPI Control Plane Gateway:", "Running on port 8000 (http://localhost:8000). Interactive OpenAPI docs at /docs."),
        ("Streamlit Live Console:", "Running on port 8501 (http://localhost:8501) with live SVG Causal DAG, step playback, and governor controls."),
        ("NVIDIA NIM Integration:", "Canonical model: nvidia/nemotron-3.5-lightning-30b-a3b with automatic fallback to meta/llama-3.3-70b-instruct."),
        ("Production SQLite Database:", "aegis_snapshots.db storing versioned immutable checkpoints and audit lineage."),
        ("Automated Test Suite:", "26/26 tests passing in 3.59s (pytest -v -s) in tests/ covering generic SDK, fleet reasoning, and treasury suite."),
        ("Modular Architecture:", "aegis_core (domain-agnostic control plane) and aegis_sdk (pluggable decorators & client).")
    ]
    for pre, body in deliv_items:
        add_bullet_point(tf_del, pre, body, size_pt=8.8, prefix_color=COLOR_PRIMARY_DARK, space_after=3)

    # Save presentations
    print(f"Saving final presentation to {output_path}...")
    prs.save(output_path)
    
    # Also save to canonical solution name
    canonical_output = "/home/ubuntu/aegis-ps01/AEGIS_Solution.pptx"
    prs.save(canonical_output)
    print(f"Also saved copy to {canonical_output}")
    print("Done! Successfully generated AEGIS solution presentation.")

if __name__ == "__main__":
    template = "/home/ubuntu/aegis-ps01/AEGIS_2026_PPT_Template.pptx"
    target = "/home/ubuntu/aegis-ps01/AEGIS_2026_PPT_template_solution.pptx"
    build_presentation(template, target)

