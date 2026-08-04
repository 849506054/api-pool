with open("/vol1/1000/tool/api-pool/test/api_pool_server.py", "r") as f:
    lines = f.readlines()

# Remove duplicate next_idx line (index 777, 0-indexed)
print(f"Removing duplicate at line 778: {repr(lines[777].rstrip())}")
del lines[777]

# Ensure blank line before _on_success
if lines[781].strip() != "":
    lines.insert(781, "\n")

with open("/vol1/1000/tool/api-pool/test/api_pool_server.py", "w") as f:
    f.writelines(lines)

print("Fixed.")
print(f"Lines 774-785: {repr(lines[774:786])}")
