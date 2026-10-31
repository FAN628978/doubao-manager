from pathlib import Path

root = Path(r"C:\Users\fht\Desktop\doubao-manager-v3")
html_path = root / "static" / "index.html"
text = html_path.read_text(encoding="utf-8")
start = text.index("  function renderJobs(){")
end = text.index("  function renderFiles(){")
assert start < end, "markers out of order"
new = (root / ".tmp" / "jobs_ui.js").read_text(encoding="utf-8")
text = text[:start] + new + text[end:]
html_path.write_text(text, encoding="utf-8")
print("spliced ok, old block", end - start, "chars ->", len(new), "chars")

# extract inline script for syntax check
script_start = text.rindex("<script>") + len("<script>")
script_end = text.rindex("</script>")
(root / ".tmp" / "index_syntax.js").write_text(text[script_start:script_end], encoding="utf-8")
print("inline js chars", script_end - script_start)
