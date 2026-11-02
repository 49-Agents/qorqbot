"""EXAMPLE verifier: checks sections and that the title mentions the product."""
import json
import sys

REQUIRED = ["hero", "proof", "cta"]


def main() -> None:
    request = json.load(sys.stdin)
    output = request["output"] if isinstance(request["output"], dict) else {}
    missing = [name for name in REQUIRED if name not in output.get("sections", [])]
    product = request["arguments"].get("product_name", "")
    if missing:
        verdict = {"passed": False, "feedback": "missing sections: " + ", ".join(missing)}
    elif product and product not in str(output.get("title", "")):
        verdict = {"passed": False, "feedback": f"the title must contain the product name {product!r}"}
    else:
        verdict = {"passed": True, "feedback": "ok"}
    json.dump(verdict, sys.stdout)


if __name__ == "__main__":
    main()
