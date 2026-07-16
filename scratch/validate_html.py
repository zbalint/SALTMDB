import urllib.request
from html.parser import HTMLParser

class MyHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, self.getpos()))

    def handle_endtag(self, tag):
        if not self.tags:
            self.errors.append(f"Unexpected close tag </{tag}> at line {self.getpos()[0]}")
            return
        last_tag, pos = self.tags.pop()
        if last_tag != tag:
            # Check if last_tag is a self-closing/void element in HTML5
            void_elements = {'img', 'input', 'br', 'hr', 'meta', 'link', 'col', 'base', 'area', 'param', 'source', 'track', 'wbr'}
            while last_tag in void_elements and self.tags:
                last_tag, pos = self.tags.pop()
            if last_tag != tag:
                self.errors.append(f"Mismatched tag: expected </{last_tag}> (opened at line {pos[0]}), found </{tag}> at line {self.getpos()[0]}")

# Fetch local landing page
url = "http://127.0.0.1:8080/"
try:
    content = urllib.request.urlopen(url).read().decode('utf-8')
    parser = MyHTMLParser()
    parser.feed(content)
    print("HTML validation completed.")
    if parser.errors:
        print("Found HTML errors:")
        for err in parser.errors:
            print(f"  - {err}")
    else:
        print("No mismatched or unclosed HTML tags found!")
except Exception as e:
    print("Failed to run HTML validator:", e)
