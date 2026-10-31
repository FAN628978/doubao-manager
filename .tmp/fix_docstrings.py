import re

p = r"C:\Users\fht\Desktop\doubao-manager-v3\web_api.py"
with open(p, encoding="utf-8") as f:
    s = f.read()

lines = s.split("\n")

def dec(m):
    return chr(int(m.group(1), 16))

for i, line in enumerate(lines):
    if "def safe(" in line:
        continue
    lines[i] = re.sub(r"\\u([0-9a-fA-F]{4})", dec, line)

with open(p, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("done")
