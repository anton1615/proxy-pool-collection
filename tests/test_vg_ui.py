import os
import re
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(REPO, "shared-mihomo", "webui", "ui.html")


class VgUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as f:
            cls.src = f.read()

    def test_script_syntax(self):
        js = re.search(r"<script>(.*?)</script>", self.src, re.S).group(1)
        p = "/tmp/ppc-vg-ui.js"
        with open(p, "w") as f:
            f.write(js)
        r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_one_rotate_button_no_quota_no_dropdown(self):
        self.assertIn("隨機切換", self.src)
        self.assertIn("api/rotate", self.src)
        self.assertNotIn("<select", self.src)
        self.assertNotIn("quota", self.src.lower())
        self.assertNotIn("country", self.src.lower())
        self.assertIn("ui-state", self.src)

    def test_relative_base_convention(self):
        self.assertIn("const BASE = location.pathname", self.src)
        self.assertIn("BASE + \"ui-state\"", self.src)


    def test_card_buttons_call_global_handlers(self):
        # A local const remove/rotate inside card() shadows the global handler;
        # onclick closures then capture the button element -> TypeError on click.
        js = re.search(r"<script>(.*?)</script>", self.src, re.S).group(1)
        harness = r'''
const calls = [];
global.document = {
  querySelector: () => ({ textContent: "", className: "", replaceChildren(){}, append(){}, set onclick(f){} }),
  createElement: () => ({ kids: [], listeners: {}, nodeType: 1,
    setAttribute(k, v){ this[k] = v }, addEventListener(k, f){ this.listeners[k] = f },
    append(...c){ this.kids.push(...c) } }),
  createTextNode: t => ({ text: t, nodeType: 3 })
};
global.location = { pathname: "/p/vg/", hostname: "h" };
global.fetch = () => Promise.reject(new Error("offline"));
global.setInterval = () => 0;
global.setTimeout = () => 0;
__JS__
rotate = uid => calls.push("rotate:" + uid);
remove = uid => calls.push("remove:" + uid);
const c = card("A", {});
const actions = c.kids.find(k => k.className === "card-actions");
actions.kids.forEach(b => b.listeners.click());
console.log(calls.join(","));
'''
        p = "/tmp/ppc-vg-ui-click.js"
        with open(p, "w") as fh:
            fh.write(harness.replace("__JS__", js))
        r = subprocess.run(["node", p], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "rotate:A,remove:A", r.stderr)

if __name__ == "__main__":
    unittest.main()
