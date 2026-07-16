with open("saltmdb_viewer.py", "r", encoding="utf-8") as f:
    content = f.read()

script_start = content.find('<script>')
script_end = content.find('</script>')
if script_start != -1 and script_end != -1:
    js = content[script_start + 8:script_end]
    with open("scratch/viewer_debug.js", "w", encoding="utf-8") as out:
        out.write(js)
    print("JS written to scratch/viewer_debug.js. Length:", len(js))
else:
    print("No script block found in file.")
