"""Tk/ttk dark desktop interface. Uses no web frontend or always-on AI process."""
from __future__ import annotations
import copy
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from pathlib import Path

from .models import QueryPlan, Projection, Relation, Filter, Sort, split_key
from .database import DatabaseGateway, ConnectionConfig, get_error_detail
from .local_ai import OllamaLocalAI, apply_ai_patch
from .sql_builder import SQLBuilder
from .security import validate_compiled_plan, check_sql_ast_if_available
from .storage import save_workspace, load_workspace

BG = "#0b1220"
TOP = "#0e192c"
PANEL = "#131f31"
PANEL2 = "#1a2940"
CANVAS = "#0e1727"
LINE = "#273750"
FOREGROUND = "#e7efff"
MUTED = "#96a9bf"
BLUE = "#17a3ed"
BLUE2 = "#176bb7"
PURPLE = "#9475f5"
GREEN = "#27d19f"
AMBER = "#ffc16c"
RED = "#ff7f8a"
FONT = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI Semibold", 10)
MONO = ("Consolas", 10)


def button(parent, text, command, role="normal", width=None):
    colors = {"normal": (PANEL2, FOREGROUND, LINE), "primary": (BLUE2, "#ffffff", BLUE2),
              "purple": ("#6b48ba", "#ffffff", "#6b48ba"),
              "success": ("#126854", "#ffffff", "#126854"),
              "subtle": (PANEL, MUTED, PANEL)}
    bg, fg, edge = colors.get(role, colors["normal"])
    kw = {"width": width} if width else {}
    return tk.Button(parent, text=text, command=command, font=FONT_SMALL,
                     bg=bg, fg=fg, activebackground=BLUE2, activeforeground="white",
                     relief="flat", bd=0, padx=12, pady=8, cursor="hand2",
                     highlightthickness=1, highlightbackground=edge, **kw)


def label(parent, text, color=FOREGROUND, font=FONT, bg=None, **kwargs):
    return tk.Label(parent, text=text, bg=bg or parent.cget("bg"), fg=color, font=font, **kwargs)


def style_root(root):
    root.configure(bg=BG)
    root.option_add("*Font", FONT)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=PANEL, foreground=FOREGROUND, font=FONT)
    style.configure("TFrame", background=PANEL)
    style.configure("Workspace.TFrame", background=BG)
    style.configure("TLabel", background=PANEL, foreground=FOREGROUND)
    style.configure("TEntry", fieldbackground=TOP, foreground=FOREGROUND, insertcolor=FOREGROUND,
                    bordercolor=LINE, lightcolor=BLUE, darkcolor=LINE, padding=(6, 5))
    style.map("TEntry", fieldbackground=[("disabled", PANEL2)])
    style.configure("TCombobox", fieldbackground=TOP, background=PANEL2, foreground=FOREGROUND,
                    arrowcolor=FOREGROUND, padding=(6, 4))
    style.map("TCombobox", fieldbackground=[("readonly", TOP)], foreground=[("readonly", FOREGROUND)])
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FOREGROUND,
                    rowheight=27, bordercolor=LINE, font=FONT_SMALL)
    style.map("Treeview", background=[("selected", "#164c77")], foreground=[("selected", "white")])
    style.configure("Treeview.Heading", background=PANEL2, foreground=FOREGROUND,
                    relief="flat", font=FONT_BOLD)
    style.configure("TNotebook", background=PANEL, borderwidth=0)
    style.configure("TNotebook.Tab", background=TOP, foreground=MUTED, padding=(11, 7))
    style.map("TNotebook.Tab", background=[("selected", PANEL2)], foreground=[("selected", "white")])
    style.configure("TCheckbutton", background=PANEL, foreground=FOREGROUND)
    style.map("TCheckbutton", background=[("active", PANEL)])
    style.configure("TRadiobutton", background=PANEL, foreground=FOREGROUND)
    style.configure("Vertical.TScrollbar", troughcolor=TOP, background=LINE, arrowcolor=MUTED)
    style.configure("Horizontal.TScrollbar", troughcolor=TOP, background=LINE, arrowcolor=MUTED)


class GraphCanvas(tk.Frame):
    """Draggable table cards, column selection and two-click column JOIN creation."""
    CARD_W = 245
    ROW_H = 29
    HEADER_H = 48
    def __init__(self, parent, app):
        super().__init__(parent, bg=CANVAS)
        self.app = app
        self.canvas = tk.Canvas(self, bg=CANVAS, highlightthickness=0, width=700,
                                height=440, scrollregion=(0, 0, 2300, 1500))
        vs = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        hs = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.drag = None
        self.port = None
        self.wire_origin = None
        self.canvas.bind("<Button-1>", self.click)
        self.canvas.bind("<Button-3>", self.context_menu)
        self.canvas.bind("<B1-Motion>", self.motion)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.canvas.bind("<MouseWheel>", self.wheel)
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
        self.render()

    def coord(self, event):
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def target_tag(self):
        found = self.canvas.find_withtag("current")
        if not found:
            return ""
        tags = self.canvas.gettags(found[-1])
        return next((t for t in tags if t.startswith(("head:", "node:", "port:", "check:", "edge:"))), "")

    def wheel(self, event):
        delta = -1 if event.delta > 0 else 1
        if event.state & 1:
            self.canvas.xview_scroll(delta * 3, "units")
        else:
            self.canvas.yview_scroll(delta * 3, "units")

    def click(self, event):
        tag = self.target_tag()
        if not tag:
            self.app.selected_key = ""
            self.app.rebuild_columns_panel()
            return
        bits = tag.split(":")
        kind = bits[0]
        if kind == "edge":
            self.app.edit_join(int(bits[1]))
            return
        try:
            i = int(bits[1])
            node = self.app.plan.nodes[i]
        except (IndexError, ValueError):
            return
        self.app.selected_key = node.key
        self.app.rebuild_columns_panel()
        if kind == "head":
            x, y = self.coord(event)
            self.drag = (node.key, x, y)
        elif kind == "check":
            ci = int(bits[2])
            table = self.app.db.tables.get(node.key)
            if table and ci < len(table.columns):
                self.app.toggle_column(node.key, table.columns[ci].name)
        elif kind == "port":
            ci = int(bits[2])
            table = self.app.db.tables.get(node.key)
            if not table or ci >= len(table.columns):
                return
            col = table.columns[ci].name
            if self.port is None:
                self.port = (node.key, col)
                self.wire_origin = self.coord(event)
                self.app.set_status("เลือกจุดเชื่อมของคอลัมน์อีกตาราง (คลิกจุดวงกลม)", AMBER)
                self.render()
            else:
                old_key, old_col = self.port
                self.port = None
                self.wire_origin = None
                if old_key != node.key:
                    self.app.create_join(old_key, old_col, node.key, col)
                self.render()
        self.render()

    def context_menu(self, event):
        tag = self.target_tag()
        bits = tag.split(":")
        if len(bits) < 2 or bits[0] not in ("head", "node", "check"):
            return
        try:
            key = self.app.plan.nodes[int(bits[1])].key
        except (ValueError, IndexError):
            return
        menu = tk.Menu(self, tearoff=False, bg=PANEL2, fg=FOREGROUND,
                       activebackground=BLUE2, activeforeground="white")
        menu.add_command(label="Set as source table", command=lambda: self.app.set_base(key))
        menu.add_command(label="Select all columns", command=lambda: self.app.select_all_for(key))
        menu.add_separator()
        menu.add_command(label="Remove table from canvas", command=lambda: self.app.remove_table(key))
        menu.tk_popup(event.x_root, event.y_root)

    def motion(self, event):
        if self.drag is None and self.port and self.wire_origin:
            self.canvas.delete("preview_wire")
            x, y = self.coord(event)
            sx, sy = self.wire_origin
            self.canvas.create_line(sx, sy, (sx+x)/2, sy, (sx+x)/2, y, x, y,
                                    smooth=True, width=3, fill=AMBER,
                                    dash=(6, 4), tags=("preview_wire",))
            return
        if self.drag is None:
            return
        key, lastx, lasty = self.drag
        x, y = self.coord(event)
        node = next((n for n in self.app.plan.nodes if n.key == key), None)
        if node:
            node.x = max(14, int(node.x + x - lastx))
            node.y = max(14, int(node.y + y - lasty))
            self.drag = key, x, y
            self.render()

    def release(self, event):
        if self.drag is None and self.port and self.wire_origin:
            x, y = self.coord(event)
            overlaps = self.canvas.find_overlapping(x - 6, y - 6, x + 6, y + 6)
            target = ""
            for item in reversed(overlaps):
                tag = next((t for t in self.canvas.gettags(item) if t.startswith("port:")), "")
                if tag:
                    target = tag
                    break
            if target:
                _, node_i, column_i = target.split(":")
                try:
                    node = self.app.plan.nodes[int(node_i)]
                    other_col = self.app.db.tables[node.key].columns[int(column_i)].name
                    old_key, old_col = self.port
                    if old_key != node.key:
                        self.port = None
                        self.wire_origin = None
                        self.canvas.delete("preview_wire")
                        self.app.create_join(old_key, old_col, node.key, other_col)
                except (ValueError, IndexError, KeyError):
                    pass
            self.canvas.delete("preview_wire")
        self.drag = None

    def auto_layout(self):
        for i, n in enumerate(self.app.plan.nodes):
            n.x, n.y = 55 + (i % 3) * 350, 90 + (i // 3) * 330
        self.render()

    def render(self):
        c = self.canvas
        c.delete("all")
        for x in range(0, 2301, 25):
            c.create_line(x, 0, x, 1500, fill="#142236")
        for y in range(0, 1501, 25):
            c.create_line(0, y, 2300, y, fill="#142236")
        plan = self.app.plan
        anchors = {}
        for node in plan.nodes:
            t = self.app.db.tables.get(node.key)
            if not t:
                continue
            for idx, col in enumerate(t.columns[:9]):
                anchors[(node.key, col.name)] = (node.x + self.CARD_W - 1, node.y + self.HEADER_H + 18 + idx * self.ROW_H,
                    node.x + 1, node.y + self.HEADER_H + 18 + idx * self.ROW_H)
        for i, j in enumerate(plan.joins):
            ka = (j.left, j.left_column)
            kb = (j.right, j.right_column)
            if ka not in anchors or kb not in anchors:
                continue
            la = anchors[ka]
            lb = anchors[kb]
            n1 = next((n for n in plan.nodes if n.key == j.left), None)
            n2 = next((n for n in plan.nodes if n.key == j.right), None)
            if not n1 or not n2:
                continue
            if n1.x <= n2.x:
                x1, y1, x2, y2 = la[0], la[1], lb[2], lb[3]
            else:
                x1, y1, x2, y2 = la[2], la[3], lb[0], lb[1]
            mid = (x1 + x2) / 2
            color = GREEN if j.kind == "EXISTS" else BLUE
            c.create_line(x1, y1, mid, y1, mid, y2, x2, y2, smooth=True,
                          width=3, fill=color, tags=(f"edge:{i}",))
            cx, cy = mid, (y1 + y2) / 2
            c.create_rectangle(cx - 36, cy - 11, cx + 36, cy + 12,
                               fill=PANEL2, outline=color, tags=(f"edge:{i}",))
            c.create_text(cx, cy, text=j.kind, font=("Segoe UI", 8, "bold"),
                          fill=color, tags=(f"edge:{i}",))
        for i, node in enumerate(plan.nodes):
            table = self.app.db.tables.get(node.key)
            if table is None:
                continue
            x, y = node.x, node.y
            visible = table.columns[:9]
            h = self.HEADER_H + len(visible) * self.ROW_H + (23 if len(table.columns) > 9 else 9)
            edge = BLUE if self.app.selected_key == node.key else LINE
            c.create_rectangle(x + 4, y + 6, x + self.CARD_W + 4, y + h + 6,
                               fill="#09111e", outline="")
            c.create_rectangle(x, y, x + self.CARD_W, y + h, fill=PANEL,
                               outline=edge, width=2)
            c.create_rectangle(x + 1, y + 1, x + self.CARD_W - 1, y + self.HEADER_H,
                               fill="#173452" if self.app.selected_key == node.key else PANEL2,
                               outline="", tags=(f"head:{i}",))
            c.create_text(x + 13, y + 20, text="▤  " + table.name[:22],
                          font=FONT_BOLD, fill=FOREGROUND, anchor="w", tags=(f"head:{i}",))
            c.create_text(x + self.CARD_W - 13, y + 20, text=node.alias,
                          font=FONT_SMALL, fill=BLUE, anchor="e", tags=(f"head:{i}",))
            c.create_text(x + 13, y + 39, text=(table.catalog + "." if table.catalog else "") + table.schema[:36], fill=MUTED,
                          font=("Segoe UI", 8), anchor="w", tags=(f"head:{i}",))
            for idx, column in enumerate(visible):
                cy = y + self.HEADER_H + 18 + idx * self.ROW_H
                tags = (f"node:{i}",)
                c.create_line(x + 1, cy - 15, x + self.CARD_W - 1, cy - 15, fill=LINE)
                is_sel = any(p.table == node.key and p.column in (column.name, "*") for p in plan.projections)
                c.create_rectangle(x + 12, cy - 6, x + 24, cy + 6,
                                   fill=BLUE2 if is_sel else CANVAS, outline=BLUE if is_sel else MUTED,
                                   tags=(f"check:{i}:{idx}",))
                if is_sel:
                    c.create_text(x + 18, cy, text="✓", fill="white", font=("Segoe UI", 9, "bold"), tags=(f"check:{i}:{idx}",))
                text = ("🔑 " if column.primary else "") + column.name
                if len(text) > 18:
                    text = text[:17] + "…"
                c.create_text(x + 35, cy, text=text, fill=FOREGROUND,
                              anchor="w", font=FONT_SMALL, tags=(f"check:{i}:{idx}",))
                c.create_text(x + self.CARD_W - 14, cy, text=column.dtype[:12],
                              fill=MUTED, anchor="e", font=("Segoe UI", 7), tags=tags)
                for px in (x + 1, x + self.CARD_W - 1):
                    selected_port = self.port == (node.key, column.name)
                    c.create_oval(px - 6, cy - 6, px + 6, cy + 6,
                                  fill=AMBER if selected_port else BLUE, outline=CANVAS,
                                  tags=(f"port:{i}:{idx}",))
            if len(table.columns) > 9:
                c.create_text(x + 13, y + h - 12,
                              text="+ " + str(len(table.columns) - 9) + " columns (use Columns panel)",
                              anchor="w", fill=MUTED, font=("Segoe UI", 8), tags=(f"node:{i}",))


class VisualSQLApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Visual SQL Studio v0.4 | Dark Pro - Read Only")
        # Fit the initial window to a typical corporate laptop, not only a big monitor.
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        width = max(990, min(1530, sw - 45))
        height = max(675, min(900, sh - 55))
        root.geometry(f"{width}x{height}")
        root.minsize(990, 675)
        style_root(root)
        self.db = DatabaseGateway()  # no connection or demo on startup
        self.plan = QueryPlan(name="New Query", dialect="mysql")
        self.selected_key = ""
        self.revision = 0
        self.result_revision = -1
        self.busy = False
        self.tasks = queue.Queue()
        self.ai_model = "AUTO"
        self.ai = OllamaLocalAI("AUTO")
        self.table_lookup = {}
        self.tested_config = None
        self._build_connection_screen()
        self.root.after(250, self._poll_tasks)

    def _empty_root(self):
        for child in self.root.winfo_children():
            child.destroy()
        self.root.configure(bg=BG)

    def _build_connection_screen(self):
        """Single-window database connection wizard: five fields, test then connect."""
        self._empty_root()
        header = tk.Frame(self.root, bg=TOP, height=68)
        header.pack(side="top", fill="x")
        header.pack_propagate(False)
        label(header, "  Visual SQL Studio", font=("Segoe UI Semibold", 18), bg=TOP).pack(side="left", padx=(20, 5), pady=16)
        label(header, "DARK PRO  /  SQL READ ONLY", color=GREEN, font=FONT_SMALL, bg=TOP).pack(side="right", padx=24)

        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill="both", expand=True, padx=24, pady=10)
        shell.grid_columnconfigure(0, weight=2)
        shell.grid_columnconfigure(1, weight=3)
        shell.grid_rowconfigure(0, weight=1)
        intro = tk.Frame(shell, bg=BG)
        intro.grid(row=0, column=0, sticky="nsew", padx=(12, 36), pady=40)
        label(intro, "DATABASE CONNECTION", color=BLUE, font=("Segoe UI Semibold", 11), bg=BG).pack(anchor="w", pady=(15, 12))
        label(intro, "เริ่มสร้าง SQL ได้ทันที", font=("Segoe UI Semibold", 22), bg=BG).pack(anchor="w", pady=(0, 12))
        label(intro, "กรอกข้อมูลเพียง 5 ช่อง แล้วกดทดสอบก่อนเชื่อมต่อ", MUTED, FONT).pack(anchor="w", pady=(0, 24))
        for number, title, detail in [
            ("01", "กรอกข้อมูลเชื่อมต่อ", "Host, User, Password, Database, Port"),
            ("02", "ทดสอบการเชื่อมต่อ", "Check the server and credentials first"),
            ("03", "เชื่อมต่อและเริ่มใช้งาน", "Browse all readable databases / schemas / tables"),
        ]:
            row = tk.Frame(intro, bg=BG)
            row.pack(fill="x", pady=12)
            label(row, number, BLUE, ("Segoe UI Semibold", 16), bg=BG).pack(side="left", padx=(0, 16))
            block = tk.Frame(row, bg=BG)
            block.pack(side="left", fill="x")
            label(block, title, font=("Segoe UI Semibold", 12), bg=BG).pack(anchor="w")
            label(block, detail, MUTED, FONT_SMALL, bg=BG).pack(anchor="w", pady=(3, 0))
        label(intro, "Read-only SQL generation  |  Local AI metadata-only", GREEN, FONT_SMALL, bg=BG).pack(anchor="w", pady=(22, 0))

        card = tk.Frame(shell, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        card.grid(row=0, column=1, sticky="nsew", padx=(0, 12), pady=0)
        top = tk.Frame(card, bg=PANEL)
        top.pack(fill="x", padx=22, pady=(10, 6))
        label(top, "เชื่อมต่อฐานข้อมูล", font=("Segoe UI Semibold", 17)).pack(anchor="w")
        label(top, "Database = default connection. All accessible databases will appear after Connect.", MUTED, FONT_SMALL).pack(anchor="w", pady=(5, 0))

        form = tk.Frame(card, bg=PANEL)
        form.pack(fill="x", padx=22, pady=(1, 2))
        form.grid_columnconfigure(1, weight=1)
        kind_var = tk.StringVar(value=self.db.config.kind)
        self.conn_kind = kind_var
        label(form, "เลือกประเภทฐานข้อมูล", MUTED, FONT).grid(row=0, column=0, sticky="w", pady=4, padx=(0, 16))
        kind_combo = ttk.Combobox(form, textvariable=kind_var, state="readonly", values=["MySQL", "PostgreSQL", "SQL Server"])
        kind_var.set({"mysql": "MySQL", "postgres": "PostgreSQL", "tsql": "SQL Server"}.get(self.db.config.kind, "MySQL"))
        kind_combo.grid(row=0, column=1, sticky="ew", pady=4)
        self.conn_fields = {}
        defaults = {"host": self.db.config.host, "user": self.db.config.user,
                    "password": "", "database": self.db.config.database,
                    "port": str(self.db.config.port or 3306)}
        for idx, (field, title) in enumerate([
            ("host", "Host"), ("user", "User"), ("password", "Password"),
            ("database", "Database"), ("port", "Port")
        ], 1):
            label(form, title, MUTED, FONT).grid(row=idx, column=0, sticky="w", pady=4, padx=(0, 16))
            v = tk.StringVar(value=defaults[field])
            self.conn_fields[field] = v
            entry = ttk.Entry(form, textvariable=v, show="*" if field == "password" else "")
            entry.grid(row=idx, column=1, sticky="ew", pady=4)
            if field == "host":
                entry.focus_set()
            v.trace_add("write", lambda *_: self._invalidate_test())
        # Keep Test and Connect ABOVE the expandable SSL panel. On laptops (720p)
        # expanding Advanced must not push the buttons below the visible window.
        commands = tk.Frame(form, bg=PANEL)
        commands.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 5))
        self.test_button = button(commands, "1. ทดสอบการเชื่อมต่อ", self._test_connection)
        self.test_button.pack(side="left", padx=(0, 9))
        self.connect_button = button(commands, "2. เชื่อมต่อฐานข้อมูล", self._connect_from_form, "primary")
        self.connect_button.pack(side="left")
        self.connect_button.configure(state="disabled")
        if self.db.connected:
            button(commands, "กลับไปหน้าทำงาน", self._back_to_workspace).pack(side="right")
        self.connection_result = label(form, "กรอกข้อมูลและกดทดสอบการเชื่อมต่อ", MUTED,
                                       FONT_SMALL, wraplength=520, justify="left")
        self.connection_result.grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 7))
        # Advanced connectivity remains hidden; the normal setup still has only 5 fields.
        self.tls_choice = tk.StringVar(value="Secure TLS (verified)")
        self.tls_expanded = tk.BooleanVar(value=False)
        self.tls_approved = False
        self.tls_toggle = ttk.Checkbutton(
            form, text="Advanced: SSL / \u0e01\u0e32\u0e23\u0e40\u0e0a\u0e37\u0e48\u0e2d\u0e21\u0e15\u0e48\u0e2d\u0e40\u0e04\u0e23\u0e37\u0e2d\u0e02\u0e48\u0e32\u0e22\u0e20\u0e32\u0e22\u0e43\u0e19",
            variable=self.tls_expanded, command=self._toggle_tls_options)
        self.tls_toggle.grid(row=8, column=0, columnspan=2, sticky="w", pady=(5, 2))
        self.tls_options = tk.Frame(form, bg=PANEL2, padx=9, pady=7)
        self.tls_menu = ttk.Combobox(
            self.tls_options, textvariable=self.tls_choice, state="readonly",
            values=["Secure TLS (verified)",
                    "Internal TLS (unverified certificate)",
                    "Legacy LAN (no encryption / IT approval)"], width=48)
        self.tls_menu.pack(fill="x")
        self.ca_choice = tk.StringVar(value="")
        ca_row = tk.Frame(self.tls_options, bg=PANEL2)
        ca_row.pack(fill="x", pady=(5, 0))
        button(ca_row, "Use CA certificate from IT (recommended)",
               self._choose_ca_file, "subtle").pack(side="left")
        self.ca_selected_label = label(ca_row, "No CA selected", MUTED, FONT_SMALL, bg=PANEL2)
        self.ca_selected_label.pack(side="left", padx=(8, 0))
        label(self.tls_options,
              "Unverified TLS keeps encryption but cannot confirm server identity. IT approval required.",
              AMBER, FONT_SMALL, bg=PANEL2, wraplength=520,
              justify="left").pack(anchor="w", pady=(3, 0))
        self.ssl_help = label(self.tls_options, "", GREEN, FONT_SMALL, bg=PANEL2,
                              wraplength=520, justify="left")
        self.ssl_help.pack(anchor="w", pady=(3, 0))
        self.tls_choice.trace_add("write", self._tls_changed)
        self._previous_type = kind_var.get()
        kind_var.trace_add("write", self._kind_changed)
        self.connection_footer = tk.Frame(card, bg=PANEL)
        self.connection_footer.pack(fill="x")
        ttk.Separator(self.connection_footer).pack(fill="x", padx=22, pady=(3, 4))
        label(self.connection_footer, "SECURITY: Use an IT/DBA-created SELECT-only account.", GREEN, FONT_SMALL).pack(anchor="w", padx=22)
        label(self.connection_footer, "Your password stays in memory. Verified TLS is the default.\n"
                    "Less secure internal connection modes require explicit approval. No rows go to AI.",
              MUTED, FONT_SMALL, justify="left").pack(anchor="w", padx=22, pady=(2, 5))

        self.status_var = tk.StringVar(value="Disconnected  |  Enter connection details and test before connecting")
        status = tk.Frame(self.root, bg=TOP, height=32)
        status.pack(side="bottom", fill="x")
        status.pack_propagate(False)
        self.status_lbl = tk.Label(status, textvariable=self.status_var, bg=TOP, fg=MUTED, anchor="w", font=FONT_SMALL)
        self.status_lbl.pack(fill="x", padx=18)

    def _toggle_tls_options(self):
        if self.tls_expanded.get():
            # Free space on low-height Windows laptop displays.
            if hasattr(self, "connection_footer"):
                self.connection_footer.pack_forget()
            self.tls_options.grid(row=9, column=0, columnspan=2,
                                  sticky="ew", pady=(5, 8))
        else:
            self.tls_options.grid_remove()
            if hasattr(self, "connection_footer"):
                self.connection_footer.pack(fill="x")

    def _tls_changed(self, *_):
        self.tls_approved = False
        self._invalidate_test()

    def _choose_ca_file(self):
        """Select a CA supplied by IT without changing the normal five-field form."""
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Select trusted database CA certificate provided by IT",
            filetypes=[("Certificate files", "*.pem *.crt *.cer"), ("All files", "*.*")],
        )
        if selected:
            self.ca_choice.set(selected)
            self.tls_choice.set("Secure TLS (verified)")
            self.ca_selected_label.configure(text=Path(selected).name[:31], fg=GREEN)
            self.ssl_help.configure(
                text="CA selected. Test using Secure TLS (verified). The server name/IP must also match its certificate.",
                fg=GREEN,
            )
            self._invalidate_test()

    def _confirm_compatibility(self, cfg):
        if cfg.tls_mode == "verify":
            return True
        if self.tls_approved:
            return True
        # Do not silently downgrade TLS even if an older CSV tool connects.
        if cfg.tls_mode == "trust":
            explanation = (
                "โหมดนี้ยังเข้ารหัสการเชื่อมต่อ TLS แต่ไม่ตรวจสอบใบรับรองของเซิร์ฟเวอร์\n"
                "จึงเสี่ยงต่อการเชื่อมต่อกับเซิร์ฟเวอร์ปลอมหรือถูกดักข้อมูล\n\n"
                "ใช้เฉพาะเซิร์ฟเวอร์ภายในบริษัทที่ฝ่าย IT ตรวจสอบและอนุมัติแล้วเท่านั้น\n"
                "คุณได้รับอนุญาตให้ใช้โหมดนี้และต้องการทดสอบต่อหรือไม่?")
        else:
            explanation = (
                "โหมดนี้ปิดการเข้ารหัส TLS อาจทำให้รหัสผ่านและข้อมูลถูกดักอ่านได้\n\n"
                "ไม่แนะนำให้ใช้เพื่อแก้ปัญหาใบรับรอง SSL ต้องได้รับการอนุมัติจาก IT\n"
                "และใช้งานบนเครือข่ายภายในที่แยกและควบคุมแล้วเท่านั้น\n"
                "คุณได้รับอนุญาตและต้องการทดสอบต่อหรือไม่?")
        answer = messagebox.askyesno("ยืนยันการตั้งค่า SSL ที่มีความเสี่ยง", explanation)
        if answer:
            self.tls_approved = True
        return answer

    def _kind_changed(self, *_):
        old = {"MySQL": 3306, "PostgreSQL": 5432, "SQL Server": 1433}
        previous = self._previous_type
        kind = self.conn_kind.get()
        if self.conn_fields["port"].get() in ("", str(old.get(previous, 3306))):
            self.conn_fields["port"].set(str(old.get(kind, 3306)))
        self._previous_type = kind
        self._invalidate_test()

    def _invalidate_test(self):
        self.tested_config = None
        self.tls_approved = False
        if hasattr(self, "connect_button") and self.connect_button.winfo_exists():
            self.connect_button.configure(state="disabled")
        if hasattr(self, "connection_result") and self.connection_result.winfo_exists():
            self.connection_result.configure(text="กรุณาทดสอบใหม่หลังแก้ไขข้อมูล", fg=MUTED)

    def _form_config(self):
        kinds = {"MySQL": "mysql", "PostgreSQL": "postgres", "SQL Server": "tsql"}
        try:
            port = int(self.conn_fields["port"].get().strip())
        except ValueError as exc:
            raise ValueError("Port must be a number, for example 3306") from exc
        cfg = ConnectionConfig(
            kind=kinds[self.conn_kind.get()],
            host=self.conn_fields["host"].get().strip(),
            user=self.conn_fields["user"].get().strip(),
            password=self.conn_fields["password"].get(),
            database=self.conn_fields["database"].get().strip(),
            port=port,
            tls_mode={
                "Secure TLS (verified)": "verify",
                "Internal TLS (unverified certificate)": "trust",
                "Legacy LAN (no encryption / IT approval)": "legacy",
            }.get(self.tls_choice.get(), "verify"),
            ca_file=self.ca_choice.get().strip() if self.tls_choice.get() == "Secure TLS (verified)" else "",
        )
        cfg.validate()
        return cfg

    def _test_connection(self):
        try:
            cfg = self._form_config()
        except ValueError as exc:
            self.connection_result.configure(text=str(exc), fg=RED)
            return
        if not self._confirm_compatibility(cfg):
            self.connection_result.configure(text="Connection test cancelled; secure defaults are unchanged.", fg=AMBER)
            return
        self.connection_result.configure(text="กำลังทดสอบการเชื่อมต่อ...", fg=BLUE)
        def done(result):
            try:
                if self._form_config() != cfg:
                    self._invalidate_test()
                    return
            except ValueError:
                self._invalidate_test()
                return
            self.tested_config = cfg
            self.connect_button.configure(state="normal")
            self.connection_result.configure(
                text="ทดสอบสำเร็จ! กดเชื่อมต่อฐานข้อมูลได้เลย", fg=GREEN)
            self.set_status("Test successful (SELECT 1 only). No tables loaded yet.", GREEN)
        def task():
            try:
                return self.db.test_connection(cfg)
            except Exception as exc:
                raise RuntimeError(get_error_detail(exc, cfg)) from None
        self.run_job(task, done, label="Testing connection (SELECT 1 only)...", secrets=(cfg.password,))

    def _connect_from_form(self):
        try:
            cfg = self._form_config()
            if self.tested_config != cfg:
                raise ValueError("Please click Test Connection first")
        except ValueError as exc:
            self.connection_result.configure(text=str(exc), fg=RED)
            return
        self.connection_result.configure(text="กำลังอ่านรายชื่อ Schema / Table / Column...", fg=BLUE)
        def done(tables):
            self.plan = QueryPlan(name="New Query", dialect=self.db.dialect)
            self.selected_key = ""
            self.revision += 1
            self.result_revision = -1
            self._build_ui()
            self.populate_tree()
            self.update_all()
            self.set_status(f"Connected: {self.db.description} - {len(tables)} tables across accessible locations", GREEN)
        def task():
            try:
                return self.db.open(cfg)
            except Exception as exc:
                raise RuntimeError(get_error_detail(exc, cfg)) from None
        self.run_job(task, done, label="Connecting and reading metadata only...", secrets=(cfg.password,))

    def _switch_to_connection(self):
        if self.busy:
            self.set_status("Finish the current action before switching connections", AMBER)
            return
        self._build_connection_screen()

    def _back_to_workspace(self):
        if not self.db.connected:
            return
        self._build_ui()
        self.populate_tree()
        self.update_all()
        self.set_status("Returned to current workspace without changing the connection", GREEN)

    def _build_ui(self):
        self._empty_root()
        root = self.root
        top = tk.Frame(root, bg=TOP, height=60)
        top.pack(side="top", fill="x")
        top.pack_propagate(False)
        tk.Label(top, text="◉", bg=TOP, fg=BLUE, font=("Segoe UI", 23, "bold")).pack(side="left", padx=(17, 5))
        label(top, "Visual SQL Studio", font=("Segoe UI Semibold", 15), bg=TOP).pack(side="left", pady=15)
        label(top, "  /  Dark Pro", color=MUTED, bg=TOP, font=FONT_SMALL).pack(side="left")
        self.connection_lbl = tk.Label(top, text="● Database Connected", bg="#123a36", fg=GREEN, font=FONT_SMALL, padx=12, pady=7)
        self.connection_lbl.pack(side="left", padx=18)
        self.safe_lbl = tk.Label(top, text="🔒 READ ONLY", bg="#153a32", fg=GREEN, font=FONT_BOLD, padx=13, pady=7)
        self.safe_lbl.pack(side="right", padx=14)
        for text, command, role in [
            ("Change DB", self._switch_to_connection, "normal"),
            ("Copy SQL", self.copy_sql, "primary"), ("Preview", self.refresh_preview, "success"),
            ("Save", self.save, "normal"), ("Open", self.load, "normal"),
            ("+ New Query", self.new_query, "normal")]:
            button(top, text, command, role).pack(side="right", padx=4, pady=8)

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)
        sidebar = tk.Frame(body, bg=PANEL, width=250, highlightthickness=1, highlightbackground=LINE)
        sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        sidebar.pack_propagate(False)
        self._build_sidebar(sidebar)

        main = tk.Frame(body, bg=BG)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(2, weight=1)
        main.grid_columnconfigure(0, weight=1)
        bar = tk.Frame(main, bg=PANEL, height=43)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        label(bar, "◇  QUERY CANVAS / ลากเชื่อม", bg=PANEL, font=FONT_BOLD).pack(side="left", padx=14)
        button(bar, "Auto layout", self.auto_layout).pack(side="right", padx=7, pady=4)
        self.sql_hint = label(bar, "Drag a table from the left, then click its connection dots", MUTED, FONT_SMALL)
        self.sql_hint.pack(side="left", padx=16)

        self._build_prompt_panel(main)
        vsplit = tk.PanedWindow(main, orient="vertical", bg=LINE, bd=0, sashwidth=7, sashrelief="flat")
        vsplit.grid(row=2, column=0, sticky="nsew")
        top_panel = tk.Frame(vsplit, bg=CANVAS)
        self.graph = GraphCanvas(top_panel, self)
        self.graph.pack(fill="both", expand=True)
        vsplit.add(top_panel, minsize=230, stretch="always")
        bottom = tk.Frame(vsplit, bg=PANEL, height=280)
        self._build_bottom(bottom)
        vsplit.add(bottom, minsize=210, stretch="never")
        # Let Tk calculate layout, then place the divider.
        root.after(200, lambda: self._set_divider(vsplit))

        right = tk.Frame(body, bg=PANEL, width=340, highlightthickness=1, highlightbackground=LINE)
        right.grid(row=0, column=2, sticky="nsew", padx=(8, 0))
        right.pack_propagate(False)
        self._build_right(right)

        status = tk.Frame(root, bg=TOP, height=30)
        status.pack(side="bottom", fill="x", pady=(7, 0))
        status.pack_propagate(False)
        self.status_var = tk.StringVar(value="Connected | Ask AI in Thai above the canvas, or add tables from the left")
        self.status_lbl = tk.Label(status, textvariable=self.status_var, bg=TOP, fg=MUTED, font=FONT_SMALL, anchor="w")
        self.status_lbl.pack(fill="x", padx=16)

    def _set_divider(self, pane):
        try:
            h = pane.winfo_height()
            if h > 500:
                pane.sash_place(0, 0, max(260, h - 300))
        except tk.TclError:
            pass

    def _build_sidebar(self, parent):
        label(parent, "DATABASE EXPLORER", font=FONT_BOLD).pack(anchor="w", padx=13, pady=(16, 7))
        search_wrap = tk.Frame(parent, bg=PANEL)
        search_wrap.pack(fill="x", padx=10)
        self.schema_search = tk.StringVar()
        self.search_entry = ttk.Entry(search_wrap, textvariable=self.schema_search)
        self.search_entry.pack(fill="x")
        self.search_entry.insert(0, "")
        self.schema_search.trace_add("write", lambda *_: self.populate_tree())
        label(parent, "Search all accessible databases / schemas / tables / columns", MUTED, ("Segoe UI", 8)).pack(anchor="w", padx=12, pady=(2, 4))
        self.explorer_count = label(parent, "", BLUE, FONT_SMALL)
        self.explorer_count.pack(anchor="w", padx=12, pady=(0, 5))
        button(parent, "↻ Refresh databases / schemas", self.refresh_metadata).pack(
            fill="x", padx=10, pady=(0, 6))
        # Connection is managed by the full-page, same-window screen.
        tree_frame = tk.Frame(parent, bg=PANEL)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=12)
        self.tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.add_from_tree())
        self.tree.bind("<<TreeviewOpen>>", self._expand_tree_item)
        self.tree.bind("<ButtonPress-1>", self._tree_start_drag)
        self.tree.bind("<ButtonRelease-1>", self._tree_drop)
        self._tree_drag = None
        button(parent, "+ Add selected table", self.add_from_tree).pack(fill="x", padx=10, pady=(0, 8))
        line = tk.Frame(parent, bg=LINE, height=1)
        line.pack(fill="x", padx=11, pady=7)
        label(parent, "QUICK TIPS", MUTED, FONT_BOLD).pack(anchor="w", padx=13, pady=(3, 8))
        tips = ("1. Double-click a table to add it\n"
                "2. Click blue dots to connect columns\n"
                "3. Click a column to show/hide it\n"
                "4. Ask AI above the canvas\n"
                "5. Preview and Copy SQL at the top")
        label(parent, tips, MUTED, FONT_SMALL, justify="left").pack(anchor="w", padx=13, pady=(0, 20))

    def _tree_start_drag(self, event):
        self._tree_drag = self.tree.identify_row(event.y)

    def _tree_drop(self, event):
        if not self._tree_drag:
            return
        # Native Tk Treeview doesn't support dropping to another widget; a button and double-click are primary.
        # Releasing over the canvas works as an easy shortcut when OS pointer coordinates overlap it.
        item = self._tree_drag
        self._tree_drag = None
        target = self.root.winfo_containing(event.x_root, event.y_root)
        widget = target
        while widget is not None:
            if widget is self.graph.canvas:
                key = self.table_lookup.get(item)
                if key:
                    x = int(self.graph.canvas.canvasx(event.x_root - self.graph.canvas.winfo_rootx()))
                    y = int(self.graph.canvas.canvasy(event.y_root - self.graph.canvas.winfo_rooty()))
                    self.add_node(key, x=max(x, 10), y=max(y, 10))
                return
            widget = getattr(widget, "master", None)

    def _build_bottom(self, parent):
        notebook = ttk.Notebook(parent)
        notebook.pack(fill="both", expand=True)
        sqlframe = tk.Frame(notebook, bg=PANEL)
        resultframe = tk.Frame(notebook, bg=PANEL)
        notebook.add(sqlframe, text="  SQL Preview  ")
        notebook.add(resultframe, text="  Real Data Preview  ")
        self.bottom_tabs = notebook
        top = tk.Frame(sqlframe, bg=PANEL)
        top.pack(fill="x", padx=12, pady=6)
        label(top, "GENERATED SQL • not editable, always built from Visual Query Plan", MUTED, FONT_SMALL).pack(side="left")
        self.validation_label = label(top, "● CHECKING", AMBER, FONT_BOLD)
        self.validation_label.pack(side="right")
        sqlpane = tk.Frame(sqlframe, bg=CANVAS)
        sqlpane.pack(fill="both", expand=True, padx=9, pady=(0, 9))
        self.sqltext = tk.Text(sqlpane, bg=CANVAS, fg="#d9e8ff", font=MONO, bd=0, relief="flat",
                               wrap="none", insertbackground=BLUE, padx=12, pady=10, state="disabled")
        self.sqltext.pack(side="left", fill="both", expand=True)
        sqlsc = ttk.Scrollbar(sqlpane, orient="vertical", command=self.sqltext.yview)
        sqlsc.pack(side="right", fill="y")
        self.sqltext.configure(yscrollcommand=sqlsc.set)
        self.sqltext.tag_configure("keyword", foreground="#73bcff")
        self.sqltext.tag_configure("string", foreground="#ffc68c")
        self.sqltext.tag_configure("comment", foreground=MUTED)
        self.sqltext.tag_configure("literal", foreground=GREEN)
        self.sqltext.tag_configure("thai", font=("Segoe UI", 11))
        rt = tk.Frame(resultframe, bg=PANEL)
        rt.pack(fill="x", padx=11, pady=7)
        self.results_caption = label(rt, "No preview yet", MUTED, FONT_SMALL)
        self.results_caption.pack(side="left")
        button(rt, "↻ Refresh (read only)", self.refresh_preview, "primary").pack(side="right")
        tbl = tk.Frame(resultframe, bg=PANEL)
        tbl.pack(fill="both", expand=True, padx=9, pady=(0, 8))
        self.results = ttk.Treeview(tbl, show="headings")
        ysc = ttk.Scrollbar(tbl, orient="vertical", command=self.results.yview)
        xsc = ttk.Scrollbar(tbl, orient="horizontal", command=self.results.xview)
        self.results.configure(yscrollcommand=ysc.set, xscrollcommand=xsc.set)
        self.results.grid(row=0, column=0, sticky="nsew")
        ysc.grid(row=0, column=1, sticky="ns")
        xsc.grid(row=1, column=0, sticky="ew")
        tbl.grid_rowconfigure(0, weight=1)
        tbl.grid_columnconfigure(0, weight=1)

    def _build_prompt_panel(self, main):
        # User's natural language (NLP) prompt lives directly above the canvas.
        panel = tk.Frame(main, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        panel.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        header = tk.Frame(panel, bg=PANEL)
        header.pack(fill="x", padx=13, pady=(9, 2))
        label(header, "ถาม AI ด้วยภาษาไทย (NLP)", color=FOREGROUND, font=FONT_BOLD).pack(side="left")
        label(header, "ไม่ส่งข้อมูลจริงให้ AI", MUTED, FONT_SMALL).pack(side="right")
        row = tk.Frame(panel, bg=PANEL)
        row.pack(fill="x", padx=12, pady=(3, 5))
        self.ai_input = tk.Text(row, bg=TOP, fg=FOREGROUND, font=FONT, relief="flat", bd=0,
                                highlightthickness=1, highlightbackground=LINE, insertbackground=BLUE,
                                wrap="word", height=3, padx=11, pady=8)
        self.ai_input.pack(side="left", fill="both", expand=True)
        label(panel, "ตัวอย่าง: แสดงชื่อและที่อยู่จากตาราง B เฉพาะเพศชาย M ที่รหัสตรงกับ A.ref", MUTED, FONT_SMALL).pack(anchor="w", padx=13, pady=(0, 3))
        controls = tk.Frame(row, bg=PANEL)
        controls.pack(side="left", padx=(9, 0))
        button(controls, "สร้าง SQL ด้วย AI", self.ask_ai, "purple").pack(fill="x", pady=(0, 4))
        button(controls, "ค้นหาโมเดลในเครื่อง", self._load_local_models).pack(fill="x")
        lower = tk.Frame(panel, bg=PANEL)
        lower.pack(fill="x", padx=13, pady=(0, 8))
        self.ai_state = label(lower, "Type your question above. AI is optional; Visual Builder works without it.", MUTED, FONT_SMALL)
        self.ai_state.pack(side="left", fill="x", expand=True)
        self.ai_model_choice = tk.StringVar(value=self.ai_model)
        model = ttk.Combobox(lower, textvariable=self.ai_model_choice,
                             state="readonly", values=["AUTO"], width=18)
        self.local_model_combo = model
        model.pack(side="right")
        model.bind("<<ComboboxSelected>>", self._change_ai_model)
        label(lower, "Local model:", MUTED, FONT_SMALL).pack(side="right", padx=(3, 7))

    def _change_ai_model(self, *_):
        self.ai_model = self.ai_model_choice.get()
        self.ai.model = self.ai_model
        self.ai_state.configure(text="Local AI model: " + self.ai_model)

    def _load_local_models(self):
        def done(models):
            self.ai_model_combo_values = ["AUTO"] + models
            # Store a direct widget reference rather than reopening any dialog.
            self.local_model_combo.configure(values=self.ai_model_combo_values)
            self.ai_state.configure(text=f"{len(models)} local model(s) found. Choose AUTO or a model.")
            self.set_status("Local AI model list updated (localhost only)", GREEN)
        self.run_job(self.ai.list_models, done, label="Checking installed local models...")

    def _build_right(self, parent):
        label(parent, "ตั้งค่า Query", font=FONT_BOLD).pack(anchor="w", padx=13, pady=(15, 9))
        ttk.Separator(parent).pack(fill="x", padx=10, pady=(0, 6))
        label(parent, "ตั้งค่า QUERY", font=FONT_BOLD).pack(anchor="w", padx=13, pady=(4, 7))
        tabs = ttk.Notebook(parent)
        tabs.pack(fill="both", expand=True, padx=8, pady=(0, 9))
        self.props_tabs = tabs
        cols = tk.Frame(tabs, bg=PANEL)
        filters = tk.Frame(tabs, bg=PANEL)
        joins = tk.Frame(tabs, bg=PANEL)
        adv = tk.Frame(tabs, bg=PANEL)
        tabs.add(cols, text="Columns")
        tabs.add(filters, text="Filters")
        tabs.add(joins, text="Join")
        tabs.add(adv, text="Advanced")
        self._build_columns(cols)
        self._build_filters(filters)
        self._build_joins(joins)
        self._build_advanced(adv)

    def _build_columns(self, p):
        self.cols_description = label(p, "Choose a table on the canvas", MUTED, FONT_SMALL)
        self.cols_description.pack(anchor="w", padx=10, pady=(13, 6))
        self.columns_holder = tk.Frame(p, bg=PANEL)
        self.columns_holder.pack(fill="both", expand=True, padx=9)
        label(p, "Select / hide output fields without changing JOIN or WHERE.", MUTED, ("Segoe UI", 8), wraplength=285, justify="left").pack(anchor="w", padx=10, pady=(8, 5))
        row = tk.Frame(p, bg=PANEL)
        row.pack(fill="x", padx=9, pady=(0, 10))
        button(row, "เลือกทุกคอลัมน์", self.select_all_columns).pack(side="left")
        button(row, "ชื่อ + ที่อยู่เท่านั้น", self.only_name_address).pack(side="left", padx=(5, 0))

    def _build_filters(self, p):
        label(p, "WHERE CONDITIONS", MUTED, FONT_BOLD).pack(anchor="w", padx=10, pady=(12, 6))
        self.filter_list = tk.Listbox(p, bg=TOP, fg=FOREGROUND, selectbackground=BLUE2,
                                     relief="flat", font=FONT_SMALL, height=5, highlightthickness=0)
        self.filter_list.pack(fill="x", padx=9, pady=3)
        row = tk.Frame(p, bg=PANEL)
        row.pack(fill="x", padx=9, pady=4)
        button(row, "Remove selected", self.remove_filter).pack(side="left")
        label(p, "Add filter", MUTED, FONT_SMALL).pack(anchor="w", padx=10, pady=(11, 3))
        self.filter_col_var = tk.StringVar()
        self.filter_col = ttk.Combobox(p, textvariable=self.filter_col_var, state="readonly")
        self.filter_col.pack(fill="x", padx=9, pady=4)
        opts = tk.Frame(p, bg=PANEL)
        opts.pack(fill="x", padx=9, pady=4)
        self.operator_var = tk.StringVar(value="=")
        self.operator_combo = ttk.Combobox(opts, textvariable=self.operator_var, state="readonly", width=13,
                                         values=["=", "!=", ">", ">=", "<", "<=", "LIKE", "IN", "BETWEEN", "IS NULL", "IS NOT NULL"])
        self.operator_combo.pack(side="left", padx=(0, 6))
        self.filter_value = tk.StringVar()
        ttk.Entry(opts, textvariable=self.filter_value).pack(side="left", fill="x", expand=True)
        self.filter_logic = tk.StringVar(value="AND")
        ttk.Combobox(p, textvariable=self.filter_logic, values=["AND", "OR"], state="readonly", width=8).pack(anchor="w", padx=9, pady=4)
        button(p, "+ Add condition", self.add_filter, "primary").pack(fill="x", padx=9, pady=8)

    def _build_joins(self, p):
        label(p, "RELATIONSHIPS", MUTED, FONT_BOLD).pack(anchor="w", padx=10, pady=(12, 6))
        label(p, "Click a blue connector dot on one column, then another dot on a different table.", MUTED,
              FONT_SMALL, wraplength=295, justify="left").pack(anchor="w", padx=10, pady=(0, 8))
        self.join_list = tk.Listbox(p, bg=TOP, fg=FOREGROUND, selectbackground=BLUE2,
                                   relief="flat", font=FONT_SMALL, height=8, highlightthickness=0)
        self.join_list.pack(fill="x", padx=9, pady=6)
        self.join_list.bind("<<ListboxSelect>>", self._select_join)
        label(p, "JOIN Type", MUTED, FONT_SMALL).pack(anchor="w", padx=10, pady=(10, 3))
        self.join_kind_var = tk.StringVar(value="INNER")
        ttk.Combobox(p, textvariable=self.join_kind_var, state="readonly",
                     values=["INNER", "LEFT", "RIGHT", "EXISTS"]).pack(fill="x", padx=9, pady=4)
        button(p, "Apply JOIN type", self._apply_join_kind, "primary").pack(fill="x", padx=9, pady=5)
        row = tk.Frame(p, bg=PANEL)
        row.pack(fill="x", padx=9)
        button(row, "Edit", self.edit_selected_join).pack(side="left")
        button(row, "Remove", self.remove_join).pack(side="left", padx=6)
        label(p, "EXISTS = matching only; avoids duplicates from the lookup table.", MUTED,
              FONT_SMALL, wraplength=285, justify="left").pack(anchor="w", padx=10, pady=13)

    def _build_advanced(self, p):
        label(p, "ADVANCED SELECT", MUTED, FONT_BOLD).pack(anchor="w", padx=10, pady=(13, 8))
        self.distinct_var = tk.BooleanVar()
        ttk.Checkbutton(p, text="DISTINCT result rows", variable=self.distinct_var,
                        command=self.change_distinct).pack(anchor="w", padx=11, pady=(0, 15))
        label(p, "Aggregate selected output column", MUTED, FONT_SMALL).pack(anchor="w", padx=10, pady=4)
        self.agg_field_var = tk.StringVar()
        self.agg_field = ttk.Combobox(p, textvariable=self.agg_field_var, state="readonly")
        self.agg_field.pack(fill="x", padx=9, pady=4)
        self.agg_var = tk.StringVar(value="COUNT")
        ttk.Combobox(p, textvariable=self.agg_var, values=["COUNT", "COUNT_DISTINCT", "SUM", "AVG", "MIN", "MAX", "NONE"], state="readonly").pack(fill="x", padx=9, pady=4)
        button(p, "Apply aggregate", self.apply_aggregate).pack(fill="x", padx=9, pady=5)
        label(p, "GROUP BY column", MUTED, FONT_SMALL).pack(anchor="w", padx=10, pady=(10, 4))
        self.group_var = tk.StringVar()
        self.group_combo = ttk.Combobox(p, textvariable=self.group_var, state="readonly")
        self.group_combo.pack(fill="x", padx=9, pady=4)
        gr = tk.Frame(p, bg=PANEL)
        gr.pack(fill="x", padx=9, pady=4)
        button(gr, "Add group", self.add_group).pack(side="left")
        button(gr, "Clear group", self.clear_group).pack(side="left", padx=(4, 0))
        label(p, "SORT / ORDER BY", MUTED, FONT_SMALL).pack(anchor="w", padx=10, pady=(11, 3))
        self.sort_var = tk.StringVar()
        self.sort_combo = ttk.Combobox(p, textvariable=self.sort_var, state="readonly", values=[])
        self.sort_combo.pack(fill="x", padx=9, pady=4)
        self.sort_direction = tk.StringVar(value="ASC")
        ttk.Combobox(p, textvariable=self.sort_direction, state="readonly", values=["ASC", "DESC"], width=8).pack(anchor="w", padx=9, pady=3)
        button(p, "Apply sort", self.apply_sort).pack(anchor="w", padx=9, pady=4)

    def refresh_metadata(self):
        """Update Explorer without prompting for credentials or mixing query contexts."""
        if self.busy or not self.db.connected:
            self.set_status("Connect first or finish the current action", AMBER)
            return
        def done(tables):
            self.populate_tree()
            self.result_revision = -1
            self.update_all()
            self.set_status(
                f"Database list refreshed: {len(tables)} tables. "
                "Existing query preserved; Preview must be refreshed.", GREEN)
        def task():
            try:
                return self.db.refresh_metadata()
            except Exception as exc:
                raise RuntimeError(get_error_detail(exc, self.db.config)) from None
        self.run_job(task, done, label="Refreshing readable databases and schemas...",
                     secrets=(self.db.config.password,))

    def _expand_tree_item(self, _event=None):
        item = self.tree.focus()
        key = self.table_lookup.get(item)
        if key:
            self._populate_table_columns(item, key, self.schema_search.get().lower().strip())

    def _populate_table_columns(self, tree_id, key, needle=""):
        existing = self.tree.get_children(tree_id)
        if len(existing) != 1 or self.tree.item(existing[0], "text") != "Loading columns...":
            return
        self.tree.delete(existing[0])
        table = self.db.tables.get(key)
        if table is None:
            return
        in_name = needle in table.qualified_name.lower()
        for col in table.columns:
            if needle and not in_name and needle not in col.name.lower():
                continue
            self.tree.insert(tree_id, "end", text=("[PK]  " if col.primary else "    ")
                             + col.name + "  :  " + col.dtype)

    def populate_tree(self):
        """Show every readable location, not just the login's default database.

        MySQL: Database -> Table -> Column
        PostgreSQL: Connected Database -> Schema -> Table -> Column
        SQL Server: Database -> Schema -> Table -> Column
        """
        if not hasattr(self, "tree"):
            return
        tree = self.tree
        tree.delete(*tree.get_children())
        self.table_lookup = {}
        needle = self.schema_search.get().lower().strip()
        database_nodes = {}
        schema_nodes = {}
        matching_tables = 0
        for key, table in sorted(self.db.tables.items(),
                                 key=lambda entry: entry[1].qualified_name.lower()):
            name_text = table.qualified_name.lower()
            if needle and needle not in name_text and not any(
                    needle in col.name.lower() for col in table.columns):
                continue
            if self.db.dialect == "mysql":
                database = table.schema
                schema = ""
            elif self.db.dialect == "postgres":
                database = self.db.config.database
                schema = table.schema
            else:
                database = table.catalog or self.db.config.database
                schema = table.schema
            if database not in database_nodes:
                database_nodes[database] = tree.insert(
                    "", "end", text="[DB]  " + database,
                    open=bool(needle) or database == self.db.config.database)
            parent = database_nodes[database]
            if schema:
                location = (database, schema)
                if location not in schema_nodes:
                    schema_nodes[location] = tree.insert(
                        parent, "end", text="[SCHEMA]  " + schema,
                        open=bool(needle) or database == self.db.config.database)
                parent = schema_nodes[location]
            table_id = tree.insert(parent, "end", text="[TABLE]  " + table.name,
                                   open=bool(needle))
            self.table_lookup[table_id] = key
            tree.insert(table_id, "end", text="Loading columns...")
            if needle:
                self._populate_table_columns(table_id, key, needle)
            matching_tables += 1
        # Group names remain tied to the user-visible permission set. A database
        # that contains no readable tables does not become a usable JOIN source.
        if hasattr(self, "explorer_count"):
            self.explorer_count.configure(text=(
                f"{len(database_nodes)} database(s)  •  {matching_tables} table(s)"
                if self.db.dialect == "mysql" else
                f"{len(database_nodes)} database(s) • {len(schema_nodes)} schema(s) • {matching_tables} table(s)"))

    def add_from_tree(self):
        item = self.tree.focus()
        key = self.table_lookup.get(item)
        if not key:
            parent = self.tree.parent(item)
            key = self.table_lookup.get(parent)
        if key:
            self.add_node(key)
        else:
            self.set_status("Double-click a table (not a schema) to add it.", AMBER)

    def add_node(self, key, x=None, y=None):
        if key not in self.db.tables:
            return
        n = len(self.plan.nodes)
        self.plan.add_node(self.db.tables[key], x if x is not None else 65 + (n % 3) * 320,
                           y if y is not None else 100 + (n // 3) * 310)
        self.selected_key = key
        self.changed()

    def remove_table(self, key):
        self.plan.nodes = [n for n in self.plan.nodes if n.key != key]
        self.plan.joins = [j for j in self.plan.joins if key not in (j.left, j.right)]
        self.plan.projections = [p for p in self.plan.projections if p.table != key]
        self.plan.filters = [f for f in self.plan.filters if f.table != key]
        self.plan.group_by = [g for g in self.plan.group_by if g[0] != key]
        if self.plan.sort and self.plan.sort.table == key:
            self.plan.sort = None
        if self.plan.base == key:
            self.plan.base = self.plan.nodes[0].key if self.plan.nodes else ""
        if not self.plan.projections and self.plan.base:
            self.plan.projections = [Projection(self.plan.base, "*")]
        self.selected_key = self.plan.base
        self.changed()

    def set_base(self, key):
        if key in [n.key for n in self.plan.nodes]:
            self.plan.base = key
            self.changed()

    def select_all_for(self, key):
        self.selected_key = key
        self.select_all_columns()

    def _active_keys(self):
        active = {self.plan.base} if self.plan.base else set()
        for j in self.plan.joins:
            if j.kind == "EXISTS":
                continue
            if j.left in active:
                active.add(j.right)
            elif j.right in active:
                active.add(j.left)
        return active

    def auto_layout(self):
        self.graph.auto_layout()
        self.set_status("Auto layout applied", GREEN)

    def toggle_column(self, key, column):
        if key not in self._active_keys():
            self.set_status("EXISTS lookup columns cannot be displayed. Switch relation to INNER JOIN first.", AMBER)
            return
        selected = self.plan.projections
        # A wildcard means all columns; clicking one narrows that table to the clicked column.
        if any(p.table == key and p.column == "*" for p in selected):
            self.plan.projections = [p for p in selected if not (p.table == key and p.column == "*")]
            self.plan.projections.append(Projection(key, column))
        elif any(p.table == key and p.column == column for p in selected):
            if len(selected) <= 1:
                self.set_status("At least one output column is required", AMBER)
                return
            self.plan.projections = [p for p in selected if not (p.table == key and p.column == column)]
        else:
            self.plan.projections.append(Projection(key, column))
        self.changed()

    def select_all_columns(self):
        if self.selected_key in self._active_keys() and self.selected_key in self.db.tables:
            self.plan.projections = [p for p in self.plan.projections if p.table != self.selected_key]
            self.plan.projections.append(Projection(self.selected_key, "*"))
            self.changed()

    def only_name_address(self):
        names = ("ชื่อ", "ที่อยู่")
        preferred = self.selected_key if self.selected_key in self.db.tables else self.plan.base
        if not preferred or not all(self.db.tables[preferred].has_column(n) for n in names):
            for key in [n.key for n in self.plan.nodes]:
                if all(self.db.tables[key].has_column(n) for n in names):
                    preferred = key
                    break
        if not preferred or not all(self.db.tables[preferred].has_column(n) for n in names):
            messagebox.showinfo("Columns", "No table on the canvas contains both columns: ชื่อ, ที่อยู่")
            return
        self.plan.projections = [Projection(preferred, n) for n in names]
        self.selected_key = preferred
        self.changed()
        self.set_status("Output changed to name and address; existing JOIN and filters preserved.", GREEN)

    def rebuild_columns_panel(self):
        if not hasattr(self, "columns_holder"):
            return
        for w in self.columns_holder.winfo_children():
            w.destroy()
        key = self.selected_key
        table = self.db.tables.get(key)
        if table is None:
            self.cols_description.configure(text="Select a table on the canvas")
            return
        self.cols_description.configure(text=f"Output from: {table.qualified_name}")
        # Scrollable column list for wide real database tables.
        canvas = tk.Canvas(self.columns_holder, bg=PANEL, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.columns_holder, orient="vertical", command=canvas.yview)
        inside = tk.Frame(canvas, bg=PANEL)
        window = canvas.create_window((0, 0), window=inside, anchor="nw")
        inside.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for c in table.columns:
            checked = any(p.table == key and p.column in (c.name, "*") for p in self.plan.projections)
            var = tk.BooleanVar(value=checked)
            cb = ttk.Checkbutton(inside, text=c.name + "  ·  " + c.dtype, variable=var,
                                 command=lambda k=key, n=c.name: self.toggle_column(k, n))
            cb.pack(anchor="w", padx=5, pady=4)

    def create_join(self, k1, c1, k2, c2):
        # New JOIN and additional ON conditions use inline controls (no pop-ups).
        for idx, j in enumerate(self.plan.joins):
            if (j.left, j.right) == (k1, k2):
                pair = (c1, c2)
            elif (j.left, j.right) == (k2, k1):
                pair = (c2, c1)
            else:
                continue
            if pair == (j.left_column, j.right_column) or pair in [tuple(x) for x in j.extras]:
                self.set_status("This relationship already exists", AMBER)
                return
            j.extras.append(pair)
            self.changed()
            self.edit_join(idx)
            self.set_status("Added an ON condition; edit JOIN type on the right", GREEN)
            return
        self.plan.joins.append(Relation(k1, c1, k2, c2, "INNER"))
        self.changed()
        self.edit_join(len(self.plan.joins) - 1)
        self.set_status("JOIN created. Choose INNER / LEFT / RIGHT / EXISTS in the Join tab", GREEN)

    def edit_join(self, idx):
        if idx < 0 or idx >= len(self.plan.joins):
            return
        self.props_tabs.select(2)
        self.join_list.selection_clear(0, "end")
        self.join_list.selection_set(idx)
        self.join_list.see(idx)
        self.join_kind_var.set(self.plan.joins[idx].kind)
        self.set_status("Edit JOIN type in the right-side Join panel", BLUE)

    def _select_join(self, *_):
        selected = self.join_list.curselection()
        if selected and selected[0] < len(self.plan.joins):
            self.join_kind_var.set(self.plan.joins[selected[0]].kind)

    def _apply_join_kind(self):
        selected = self.join_list.curselection()
        if not selected:
            self.set_status("Select a relationship in the Join list first", AMBER)
            return
        new_kind = self.join_kind_var.get()
        if new_kind not in ("INNER", "LEFT", "RIGHT", "EXISTS"):
            return
        self.plan.joins[selected[0]].kind = new_kind
        self.changed()
        self.set_status("JOIN type updated. SQL was regenerated without running a preview.", GREEN)

    def edit_selected_join(self):
        sel = self.join_list.curselection()
        if sel:
            self.edit_join(sel[0])

    def remove_join(self):
        sel = self.join_list.curselection()
        if sel:
            del self.plan.joins[sel[0]]
            self.changed()

    def add_filter(self):
        ref = self.filter_col_var.get()
        choice = self._column_options().get(ref)
        if not choice:
            messagebox.showinfo("Filter", "Select a table column first")
            return
        key, col = choice
        self.plan.filters.append(Filter(key, col, self.operator_var.get(),
                                        self.filter_value.get(), self.filter_logic.get()))
        self.filter_value.set("")
        self.changed()

    def remove_filter(self):
        sel = self.filter_list.curselection()
        if sel:
            del self.plan.filters[sel[0]]
            self.changed()

    def _column_options(self):
        opts = {}
        for node in self.plan.nodes:
            table = self.db.tables.get(node.key)
            if table and node.key in self._active_keys():
                for column in table.columns:
                    opts[f"{node.alias}.{column.name}"] = (node.key, column.name)
        return opts

    def change_distinct(self):
        self.plan.distinct = self.distinct_var.get()
        self.changed()

    def apply_aggregate(self):
        ref = self.agg_field_var.get()
        opts = self._column_options()
        if ref not in opts:
            return
        key, col = opts[ref]
        idx = next((i for i,p in enumerate(self.plan.projections) if p.table == key and p.column == col), None)
        if idx is None:
            self.plan.projections.append(Projection(key, col))
            idx = len(self.plan.projections) - 1
        self.plan.projections[idx].aggregate = "" if self.agg_var.get() == "NONE" else self.agg_var.get()
        self.changed()

    def add_group(self):
        pair = self._column_options().get(self.group_var.get())
        if pair and pair not in self.plan.group_by:
            self.plan.group_by.append(pair)
            self.changed()

    def clear_group(self):
        self.plan.group_by.clear()
        self.changed()

    def apply_sort(self):
        pair = self._column_options().get(self.sort_var.get())
        if pair:
            self.plan.sort = Sort(pair[0], pair[1], self.sort_direction.get())
            self.changed()

    def changed(self):
        self.revision += 1
        self.update_all()
        if self.result_revision >= 0 and self.result_revision != self.revision:
            self.results_caption.configure(text="⚠ Result is from an older query. Click Refresh.", fg=AMBER)

    def update_all(self):
        self.graph.render()
        self.rebuild_columns_panel()
        self.distinct_var.set(self.plan.distinct)
        options = self._column_options()
        names = list(options)
        for comb, variable in [(self.filter_col, self.filter_col_var), (self.agg_field, self.agg_field_var),
                               (self.group_combo, self.group_var)]:
            comb.configure(values=names)
            if names and variable.get() not in names:
                variable.set(names[0])
        self.sort_combo.configure(values=names)
        if names and self.sort_var.get() not in names:
            self.sort_var.set(names[0])
        self.filter_list.delete(0, "end")
        for i,f in enumerate(self.plan.filters):
            self.filter_list.insert("end", f"{f.logical if i else 'WHERE'} {self.plan.alias_for(f.table)}.{f.column} {f.operator} {f.value}")
        self.join_list.delete(0, "end")
        for j in self.plan.joins:
            a = self.db.tables.get(j.left)
            b = self.db.tables.get(j.right)
            self.join_list.insert("end", f"{j.kind}  {a.qualified_name if a else '?'}.{j.left_column} ↔ {b.qualified_name if b else '?'}.{j.right_column}" + (f"  +{len(j.extras)}" if j.extras else ""))
        sql, report = validate_compiled_plan(self.plan, self.db.tables)
        if report.safe:
            report = check_sql_ast_if_available(sql, self.db.dialect, required=True)
        self.last_sql = sql
        self.last_report = report
        if report.safe:
            self.validation_label.configure(text="● READ ONLY ✓", fg=GREEN)
        else:
            self.validation_label.configure(text="● INCOMPLETE / BLOCKED", fg=AMBER)
        self.sqltext.configure(state="normal")
        self.sqltext.delete("1.0", "end")
        self.sqltext.insert("1.0", sql if sql else "-- " + report.message + "\n-- Add tables and select output columns to get started.")
        self._highlight_sql()
        self.sqltext.configure(state="disabled")
        self.connection_lbl.configure(text="● " + self.db.description)

    def _highlight_sql(self):
        import re
        text = self.sqltext.get("1.0", "end")
        keywords = r"\b(?:SELECT|DISTINCT|FROM|AS|WHERE|AND|OR|JOIN|INNER|LEFT|RIGHT|ON|EXISTS|GROUP|BY|ORDER|ASC|DESC|BETWEEN|IS|NULL|IN|LIKE|COUNT|SUM|AVG|MIN|MAX|TOP|LIMIT)\b"
        for m in re.finditer(keywords, text, re.I):
            self.sqltext.tag_add("keyword", "1.0+%dc"%m.start(), "1.0+%dc"%m.end())
        for m in re.finditer(r"'(?:''|[^'])*'", text):
            self.sqltext.tag_add("string", "1.0+%dc"%m.start(), "1.0+%dc"%m.end())
        for m in re.finditer("[\u0E00-\u0E7F]+", text):
            self.sqltext.tag_add("thai", "1.0+%dc"%m.start(), "1.0+%dc"%m.end())
        if text.startswith("--"):
            self.sqltext.tag_add("comment", "1.0", "end")

    def new_query(self):
        self.plan = QueryPlan(name="New Query", dialect=self.db.dialect)
        self.selected_key = ""
        self.ai_input.delete("1.0", "end")
        self.graph.port = None
        self.revision += 1
        self.result_revision = -1
        self.clear_results()
        self.update_all()
        self.set_status("New query: all previous JOINs, filters and AI context are cleared", GREEN)

    def clear_results(self):
        self.results.delete(*self.results.get_children())
        self.results.configure(columns=[])
        self.results_caption.configure(text="No preview yet", fg=MUTED)

    def refresh_preview(self):
        if not self.last_report.safe:
            messagebox.showwarning("Query not ready", self.last_report.message)
            return
        rev = self.revision
        snap = copy.deepcopy(self.plan)
        def task():
            return self.db.preview(snap, 100)
        def done(data):
            if rev != self.revision:
                self.set_status("Old preview discarded because this Query changed", AMBER)
                return
            cols, rows = data
            self.results.delete(*self.results.get_children())
            keys = [f"c{i}" for i in range(len(cols))]
            self.results.configure(columns=keys)
            for i, name in enumerate(cols):
                self.results.heading(keys[i], text=name)
                self.results.column(keys[i], width=max(115, min(220, len(name) * 13 + 35)), anchor="w")
            for r in rows:
                self.results.insert("", "end", values=[str(v) if v is not None else "NULL" for v in r])
            self.result_revision = rev
            prefix = "REAL DB READ-ONLY"
            self.results_caption.configure(text=f"{prefix} • {len(rows)} row(s) shown • limit 100", fg=GREEN if rev == self.revision else AMBER)
            if rev != self.revision:
                self.results_caption.configure(text="⚠ Query changed during preview. Refresh for current results", fg=AMBER)
            self.bottom_tabs.select(1)
            self.set_status(f"Preview complete: {len(rows)} rows • no data sent to AI", GREEN)
        self.run_job(task, done, label="Fetching read-only preview (max 100 rows)...")

    def copy_sql(self):
        if not self.last_report.safe:
            messagebox.showwarning("Blocked", "SQL is not ready or does not pass the read-only safety checks.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.last_sql)
        self.set_status("Validated, builder-generated SQL copied to clipboard", GREEN)

    def save(self):
        if not self.plan.nodes:
            messagebox.showinfo("Save", "Add at least one table before saving")
            return
        name = filedialog.asksaveasfilename(title="Save SQL Workspace", defaultextension=".vsql",
            filetypes=[("Visual SQL Workspace", "*.vsql")], initialfile="My_Query.vsql")
        if name:
            try:
                save_workspace(name, self.plan)
                self.set_status("Saved workspace (no credentials or preview data)", GREEN)
            except Exception as e:
                messagebox.showerror("Save failed", str(e))

    def load(self):
        filename = filedialog.askopenfilename(filetypes=[("Visual SQL Workspace", "*.vsql")])
        if not filename:
            return
        try:
            p = load_workspace(filename)
            if p.dialect != self.db.dialect:
                raise ValueError("This workspace needs a different database type. Connect to it first.")
            missing = [n.key for n in p.nodes if n.key not in self.db.tables]
            if missing:
                raise ValueError("Some tables are missing from the current connection. Open the correct database first.")
            self.plan = p
            self.selected_key = p.base
            self.revision += 1
            self.result_revision = -1
            self.clear_results()
            self.update_all()
            self.set_status("Workspace loaded with its own isolated query context", GREEN)
        except Exception as e:
            messagebox.showerror("Open failed", str(e))

    def ask_ai(self):
        question = self.ai_input.get("1.0", "end").strip()
        if not question:
            messagebox.showinfo("AI", "Describe the data you want, in Thai or English")
            return
        if not self.ai_model:
            messagebox.showinfo("Local AI", "Install Ollama and a local model, then click Find local models. You can also build SQL manually.")
            return
        plan_snapshot = copy.deepcopy(self.plan)
        tables = dict(self.db.tables)
        rev = self.revision
        def task():
            return self.ai.propose(question, tables, plan_snapshot)
        def done(patch):
            if rev != self.revision:
                messagebox.showwarning("Query changed", "Query changed while AI was working. Please ask again to avoid mixing contexts.")
                return
            try:
                proposed = apply_ai_patch(plan_snapshot, patch, tables)
                summary = str(patch.get("explanation", ""))[:600]
                newcols = ", ".join(p.column for p in proposed.projections[:12])
                preview = f"{summary}\n\nOutput columns: {newcols}\nFilters: {len(proposed.filters)}\nRelationships: {len(proposed.joins)}\n\nApply this proposed change?"
                if messagebox.askyesno("Review AI proposal", preview):
                    proposed.user_context = plan_snapshot.user_context[-4:] + [question[:400]]
                    self.plan = proposed
                    self.selected_key = proposed.base
                    self.changed()
                    self.set_status("AI proposal applied; inspect SQL and click Refresh Preview", GREEN)
            except Exception as exc:
                messagebox.showerror("AI plan rejected", str(exc) + "\n\nThe AI cannot execute arbitrary SQL.")
        self.run_job(task, done, label="Local AI is planning (schema only; no rows)...")

    def run_job(self, task, done, label="Working...", secrets=()):
        if self.busy:
            self.set_status("Another task is running. Wait until it completes.", AMBER)
            return
        self.busy = True
        self.set_status(label, BLUE)
        def worker():
            try:
                value = task()
                self.tasks.put((True, value, done))
            except Exception as exc:
                # Strip in-memory database password if a driver included it in an error.
                err = str(exc)
                for pw in list(secrets) + [self.db.config.password]:
                    if pw:
                        err = err.replace(pw, "[hidden]")
                self.tasks.put((False, err, done))
        threading.Thread(target=worker, daemon=True).start()

    def _poll_tasks(self):
        try:
            while True:
                ok, value, callback = self.tasks.get_nowait()
                self.busy = False
                if ok:
                    try:
                        callback(value)
                    except Exception as exc:
                        messagebox.showerror("Application error", str(exc))
                else:
                    self.set_status("Operation failed: " + value[:150], RED)
                    if hasattr(self, "connection_result") and self.connection_result.winfo_exists() and \
                            self.connection_result.winfo_ismapped():
                        self.tested_config = None
                        self.connect_button.configure(state="disabled")
                        # Keep the actionable summary visible without the whole driver's
                        # multi-line traceback pushing the Test/Connect controls down.
                        ssl_problem = any(x in value.lower() for x in ("ssl", "tls", "certificate"))
                        summary = ("SSL certificate not trusted. Open Advanced: select a CA from IT "
                                   "or use Internal TLS only with IT approval. Then Test again.") if ssl_problem else value[:215]
                        self.connection_result.configure(text="Connection failed: " + summary, fg=RED)
                        if ssl_problem:
                            self.tls_expanded.set(True)
                            self._toggle_tls_options()
                            self.ssl_help.configure(
                                text=("SSL certificate verification failed. Ask IT for a CA certificate, "
                                      "then click 'Use CA certificate from IT'. "
                                      "Alternatively, ONLY with IT approval, select "
                                      "'Internal TLS (unverified certificate)' and Test again."),
                                fg=AMBER,
                            )
                    else:
                        messagebox.showerror("Operation failed", value[:1400])
        except queue.Empty:
            pass
        self.root.after(150, self._poll_tasks)

    def set_status(self, message, color=MUTED):
        self.status_var.set(message)
        self.status_lbl.configure(fg=color)


def launch():
    root = tk.Tk()
    app = VisualSQLApp(root)
    root.mainloop()
    app.db.close()
