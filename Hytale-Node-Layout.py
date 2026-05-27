import json
import os
import datetime
from collections import defaultdict, deque

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext


# ============================================================================
# CONFIG
# ============================================================================

GRID_X = 320

LANE_Y = 180

Y_LANE_THRESHOLD = 70
X_STANDARD_THRESHOLD = 80

SPLIT_X_THRESHOLD = 140

COLLISION_PADDING = 120


# ============================================================================
# NODE
# ============================================================================

class Node:
    def __init__(self, node_id, data, metadata=None):
        self.id = node_id
        self.data = data
        self.meta = metadata or {}

        pos = self.meta.get("$Position", {})

        self.original_position = {
            "x": pos.get("$x", 0),
            "y": pos.get("$y", 0)
        }

        self.position = dict(self.original_position)

        self.normalized_position = {
            "x": 0,
            "y": 0
        }

        self.final_position = {
            "x": 0,
            "y": 0
        }

        self.children = []
        self.parents = []
        self.edges = []

        self.lane = None

    def apply_position(self):
        if "$Position" not in self.meta:
            self.meta["$Position"] = {}

        self.meta["$Position"]["$x"] = int(round(self.position["x"]))
        self.meta["$Position"]["$y"] = int(round(self.position["y"]))


# ============================================================================
# EDGE
# ============================================================================

class Edge:
    def __init__(self, source, target, field):
        self.source = source
        self.target = target
        self.field = field


# ============================================================================
# LAYOUT ENGINE
# ============================================================================

class HytaleFlowLayoutEngine:

    def __init__(self, logger=None):
        self.logger = logger

        self.nodes = {}
        self.edges = []

        self.root_id = None
        self.lanes = []

    # ------------------------------------------------------------------------
    # LOG
    # ------------------------------------------------------------------------

    def log(self, message):
        print(message)

        if self.logger:
            self.logger(message)

    # ------------------------------------------------------------------------
    # LOAD GRAPH
    # ------------------------------------------------------------------------

    def load_graph(self, json_data):

        self.nodes = {}
        self.edges = []

        metadata_nodes = (
            json_data
            .get("$NodeEditorMetadata", {})
            .get("$Nodes", {})
        )

        def recurse(obj, parent_id=None, field_name=None):

            if field_name == "$NodeEditorMetadata":
                return

            if isinstance(obj, dict):

                current_parent = parent_id

                if "$NodeId" in obj:

                    node_id = obj["$NodeId"]

                    if node_id not in self.nodes:

                        meta = metadata_nodes.get(node_id, {})

                        self.nodes[node_id] = Node(
                            node_id,
                            obj,
                            meta
                        )

                    if parent_id and parent_id != node_id:

                        edge = Edge(
                            parent_id,
                            node_id,
                            field_name
                        )

                        self.edges.append(edge)

                        self.nodes[parent_id].children.append(node_id)
                        self.nodes[parent_id].edges.append(edge)

                        self.nodes[node_id].parents.append(parent_id)

                    current_parent = node_id

                for key, value in obj.items():
                    recurse(value, current_parent, key)

            elif isinstance(obj, list):

                for item in obj:
                    recurse(item, parent_id, field_name)

        recurse(json_data)

        self.log(f"Loaded {len(self.nodes)} nodes")
        self.log(f"Extracted {len(self.edges)} edges")

        self.detect_root()

    # ------------------------------------------------------------------------
    # ROOT
    # ------------------------------------------------------------------------

    def detect_root(self):

        roots = []

        for node_id, node in self.nodes.items():

            if not node.parents:
                roots.append(node_id)

        if not roots:
            self.root_id = next(iter(self.nodes.keys()))
            return

        roots.sort(
            key=lambda nid: (
                -len(self.nodes[nid].children),
                self.nodes[nid].original_position["x"]
            )
        )

        self.root_id = roots[0]

        self.log(f"Root node: {self.root_id}")

    # ------------------------------------------------------------------------
    # NORMALIZE
    # ------------------------------------------------------------------------

    def normalize_positions(self):

        root = self.nodes[self.root_id]

        root_x = root.original_position["x"]
        root_y = root.original_position["y"]

        for node in self.nodes.values():

            nx = node.original_position["x"] - root_x
            ny = node.original_position["y"] - root_y

            node.normalized_position["x"] = nx
            node.normalized_position["y"] = ny

        self.log("Normalized positions")

    # ------------------------------------------------------------------------
    # BUILD LANES
    # ------------------------------------------------------------------------

    def build_lanes(self):

        y_values = sorted([
            node.normalized_position["y"]
            for node in self.nodes.values()
        ])

        lanes = []

        for y in y_values:

            placed = False

            for lane in lanes:

                if abs(y - lane["anchor"]) <= Y_LANE_THRESHOLD:

                    lane["values"].append(y)

                    lane["anchor"] = round(
                        sum(lane["values"]) / len(lane["values"])
                    )

                    placed = True
                    break

            if not placed:

                lanes.append({
                    "anchor": y,
                    "values": [y]
                })

        self.lanes = sorted([
            lane["anchor"]
            for lane in lanes
        ])

        self.log(f"Generated {len(self.lanes)} lanes")

    # ------------------------------------------------------------------------
    # ASSIGN LANES
    # ------------------------------------------------------------------------

    def assign_lanes(self):

        for node in self.nodes.values():

            y = node.normalized_position["y"]

            nearest = min(
                self.lanes,
                key=lambda lane_y: abs(lane_y - y)
            )

            node.lane = nearest

    # ------------------------------------------------------------------------
    # X NORMALIZATION
    # ------------------------------------------------------------------------

    def adjusted_dx(self, dx):

        if abs(dx - GRID_X) <= X_STANDARD_THRESHOLD:
            return GRID_X

        return dx

    # ------------------------------------------------------------------------
    # PROPAGATE
    # ------------------------------------------------------------------------

    def propagate_layout(self):

        visited = set()

        # ------------------------------------------------------------
        # CONFIG (tuning knobs)
        # ------------------------------------------------------------

        X_SPLIT_THRESHOLD = GRID_X * 0.6   # when a new column is considered real
        X_SNAP_SOFT = GRID_X * 0.25        # small jitter ignored

        # ------------------------------------------------------------
        # ROOT INIT
        # ------------------------------------------------------------

        root = self.nodes[self.root_id]

        root.final_position["x"] = 0
        root.final_position["y"] = 0

        root.column = 0

        queue = deque([root.id])

        # ------------------------------------------------------------
        # BFS PROPAGATION
        # ------------------------------------------------------------

        while queue:

            node_id = queue.popleft()

            if node_id in visited:
                continue

            visited.add(node_id)

            node = self.nodes[node_id]

            if not hasattr(node, "column"):
                node.column = 0

            groups = defaultdict(list)

            for edge in node.edges:

                child = self.nodes[edge.target]

                dx = child.original_position["x"] - node.original_position["x"]

                if abs(dx) <= X_SNAP_SOFT:
                    col = node.column

                elif abs(dx - GRID_X) <= X_SPLIT_THRESHOLD:
                    col = node.column + 1

                else:
                    col = node.column + int(round(dx / GRID_X))

                groups[col].append(child)

            # ------------------------------------------------------------
            # APPLY GROUPS
            # ------------------------------------------------------------

            for col, children in groups.items():

                children.sort(
                    key=lambda c: c.original_position["y"]
                )

                for child in children:
                    child.column = col

                    child.final_position["x"] = col * GRID_X

                    child.final_position["y"] = self.adjust_y_with_lane(
                        node,
                        child
                    )

                    queue.append(child.id)

        # ------------------------------------------------------------
        # FINAL NORMALIZATION PASS (small cleanup)
        # ------------------------------------------------------------

        self.smooth_columns()

    def adjust_y_with_lane(self, parent, child):

        dy = child.original_position["y"] - parent.original_position["y"]

        if abs(dy) < LANE_Y * 0.6:
            return parent.final_position["y"]

        return parent.final_position["y"] + dy
    
    def smooth_columns(self):

        for node in self.nodes.values():
            if not hasattr(node, "column"):
                continue

            node.column = round(node.column)
    # ------------------------------------------------------------------------
    # COLLISIONS
    # ------------------------------------------------------------------------

    def resolve_collisions(self):

        occupied = defaultdict(list)

        for node in self.nodes.values():

            key = (
                round(node.final_position["x"]),
                round(node.final_position["y"])
            )

            occupied[key].append(node)

        for key, nodes in occupied.items():

            if len(nodes) <= 1:
                continue

            for i, node in enumerate(nodes):

                node.final_position["y"] += (
                    i * COLLISION_PADDING
                )

        self.log("Resolved collisions")

    # ------------------------------------------------------------------------
    # COMMIT
    # ------------------------------------------------------------------------

    def commit_positions(self):

        root_original = (
            self.nodes[self.root_id]
            .original_position
        )

        root_x = root_original["x"]
        root_y = root_original["y"]

        for node in self.nodes.values():

            node.position["x"] = int(
                root_x + node.final_position["x"]
            )

            node.position["y"] = int(
                root_y + node.final_position["y"]
            )

            node.apply_position()

        self.log("Committed positions")

    # ------------------------------------------------------------------------
    # RUN
    # ------------------------------------------------------------------------

    def run_layout(self):

        self.normalize_positions()

        self.build_lanes()

        self.assign_lanes()

        self.propagate_layout()

        self.resolve_collisions()

        self.commit_positions()

        self.log("Layout complete")

    # ------------------------------------------------------------------------
    # EXPORT
    # ------------------------------------------------------------------------

    def export_json(self, original_data):
        return original_data


# ============================================================================
# UI
# ============================================================================

class LayoutUI:

    def __init__(self, root):

        self.root = root

        self.root.title("Hytale-Node-Layout")
        self.root.geometry("1000x700")

        self.current_file = None

        self.original_data = None
        self.modified_data = None

        self.engine = HytaleFlowLayoutEngine(
            logger=self.log
        )

        self.build_ui()

    # ------------------------------------------------------------------------
    # BUILD UI
    # ------------------------------------------------------------------------

    def build_ui(self):

        top = tk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=10)

        open_btn = tk.Button(
            top,
            text="Open JSON",
            width=20,
            height=2,
            command=self.open_json
        )
        open_btn.pack(side="left", padx=5)

        run_btn = tk.Button(
            top,
            text="Run Layout",
            width=20,
            height=2,
            command=self.run_layout
        )
        run_btn.pack(side="left", padx=5)

        save_btn = tk.Button(
            top,
            text="Save JSON",
            width=20,
            height=2,
            command=self.save_json
        )
        save_btn.pack(side="left", padx=5)

        self.log_window = scrolledtext.ScrolledText(
            self.root,
            wrap=tk.WORD,
            font=("Consolas", 10)
        )

        self.log_window.pack(
            fill="both",
            expand=True,
            padx=10,
            pady=10
        )

    # ------------------------------------------------------------------------
    # LOG
    # ------------------------------------------------------------------------

    def log(self, message):

        self.log_window.insert(
            tk.END,
            message + "\n"
        )

        self.log_window.see(tk.END)

        self.root.update_idletasks()

    # ------------------------------------------------------------------------
    # OPEN
    # ------------------------------------------------------------------------

    def open_json(self):

        path = filedialog.askopenfilename(
            filetypes=[("JSON Files", "*.json")]
        )

        if not path:
            return

        try:

            with open(path, "r", encoding="utf-8") as f:
                self.original_data = json.load(f)

            self.current_file = path

            self.engine.load_graph(self.original_data)

            self.log(f"Opened: {path}")

        except Exception as e:

            messagebox.showerror(
                "Error",
                str(e)
            )

    # ------------------------------------------------------------------------
    # BACKUP
    # ------------------------------------------------------------------------

    def create_backup(self):

        if not self.current_file:
            return

        backup_dir = os.path.join(
            os.path.dirname(
                os.path.abspath(__file__)
            ),
            "backup"
        )

        os.makedirs(
            backup_dir,
            exist_ok=True
        )

        filename = os.path.basename(
            self.current_file
        )

        name, ext = os.path.splitext(filename)

        timestamp = datetime.datetime.now().strftime(
            "%Y%m%d_%H%M%S"
        )

        backup_path = os.path.join(
            backup_dir,
            f"{name}_{timestamp}{ext}"
        )

        with open(
            backup_path,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                self.original_data,
                f,
                indent=2
            )

        self.log(f"Backup created: {backup_path}")

    # ------------------------------------------------------------------------
    # RUN
    # ------------------------------------------------------------------------

    def run_layout(self):

        if not self.original_data:

            messagebox.showwarning(
                "Warning",
                "Open a JSON first"
            )

            return

        try:

            self.create_backup()

            self.engine.run_layout()

            self.modified_data = self.engine.export_json(
                self.original_data
            )

            self.log("Layout finished")


        except Exception as e:

            self.log(str(e))

            messagebox.showerror(
                "Error",
                str(e)
            )

    # ------------------------------------------------------------------------
    # SAVE
    # ------------------------------------------------------------------------

    def save_json(self):

        if not self.modified_data:

            messagebox.showwarning(
                "Warning",
                "Run layout first"
            )

            return

        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON Files", "*.json")]
        )

        if not path:
            return

        try:

            with open(path, "w", encoding="utf-8") as f:

                json.dump(
                    self.modified_data,
                    f,
                    indent=2
                )

            self.log(f"Saved: {path}")

        except Exception as e:

            messagebox.showerror(
                "Error",
                str(e)
            )


# ============================================================================
# MAIN
# ============================================================================

def main():

    root = tk.Tk()

    app = LayoutUI(root)

    root.mainloop()


if __name__ == "__main__":
    main()