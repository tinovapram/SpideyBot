"""
SpideyBot — Formatting Utilities.

File tree rendering and display formatting for Telegram messages.
"""

from typing import List


def format_filetree(files, title: str) -> str:
    """
    Format a list of TeraBoxFile objects into a tree structure for display,
    stripping parent directories for cleaner output.

    Args:
        files: List of TeraBoxFile objects (must have .path, .is_dir, .size_mb, .fs_id).
        title: Title for the tree (unused in output but reserved for future use).

    Returns:
        A formatted string representing the file tree with emoji icons.
    """
    valid_paths = [f.path for f in files if f.path]
    if not valid_paths:
        return ""

    valid_paths.sort(key=lambda p: p.count('/'))
    root_path = valid_paths[0]

    parts = root_path.split('/')
    parent_prefix = '/'.join(parts[:-1]) + '/'

    tree = {}
    for f in files:
        path = f.path
        if path.startswith(parent_prefix):
            path = path[len(parent_prefix):]

        parts = [p for p in path.split('/') if p]
        if not parts:
            continue

        current = tree
        for part in parts[:-1]:
            if part not in current:
                current[part] = {"is_dir": True, "children": {}}
            current = current[part]["children"]

        name = parts[-1]
        if f.is_dir:
            if name not in current:
                current[name] = {"is_dir": True, "children": {}}
        else:
            current[name] = {
                "is_dir": False,
                "size_mb": f.size_mb,
                "fs_id": f.fs_id
            }

    def render(node, prefix=""):
        lines = []
        items = sorted(node.items(), key=lambda x: (not x[1]["is_dir"], x[0].lower()))
        for i, (name, data) in enumerate(items):
            is_last = (i == len(items) - 1)
            connector = "└── " if is_last else "├── "

            if data["is_dir"]:
                lines.append(f"{prefix}{connector}📁 {name}/")
                new_prefix = prefix + ("    " if is_last else "│   ")
                lines.extend(render(data["children"], new_prefix))
            else:
                size_str = f" ({data['size_mb']:.2f} MB)" if data.get('size_mb') is not None else ""
                lines.append(f"{prefix}{connector}📄 {name}{size_str}")
        return lines

    rendered_lines = render(tree)
    return "\n".join(rendered_lines)
