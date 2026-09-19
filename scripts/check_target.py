# #!/usr/bin/env python3
# """
# Vet a candidate site BEFORE you build your demo around it.

#     python scripts/check_target.py https://example.ac.in/

# Checks, in order:
#   1. Does robots.txt allow us?
#   2. Does it respond at all, and how slowly?
#   3. What does the mirrored copy score?  (30-55 is the sweet spot for a demo:
#      bad enough to be worth fixing, not so broken it looks staged)
# """
# import sys
# from pathlib import Path
# from urllib.parse import urljoin
# from urllib.robotparser import RobotFileParser

# sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# import time
# import httpx
# from audit.mirror import USER_AGENT, mirror
# from audit.runner import audit_html
# from audit.scorer import score_from_violations


# def robots_allows(url: str) -> bool:
#     robots_url = urljoin(url, "/robots.txt")
#     parser = RobotFileParser()
#     try:
#         response = httpx.get(robots_url, timeout=10, headers={"User-Agent": USER_AGENT})
#         parser.parse(response.text.splitlines())
#     except Exception as error:
#         print(f"  robots.txt unreadable ({error}); treating as allowed")
#         return True
#     return parser.can_fetch(USER_AGENT, url)


# def main() -> int:
#     url = sys.argv[1]
#     print(f"checking {url}")

#     if not robots_allows(url):
#         print("  BLOCKED by robots.txt -- pick another target")
#         return 1
#     print("  robots.txt: allowed")

#     started = time.time()
#     mirrored = mirror(url)
#     print(f"  fetched in {time.time() - started:.1f}s [{mirrored.status_code}] "
#           f"{len(mirrored.html)} bytes")
#     for note in mirrored.notes:
#         print(f"    {note}")

#     result = audit_html(mirrored.html, take_screenshot=False)
#     score = score_from_violations(result.violations)
#     print(f"  score: {score}/100, {len(result.violations)} rules failing")

#     if score > 85:
#         print("  -> too accessible already, boring demo")
#     elif score < 20:
#         print("  -> extremely broken; good impact, but check the mirror renders correctly")
#     else:
#         print("  -> GOOD DEMO CANDIDATE")

#     print("  top rules:")
#     for violation in sorted(result.violations,
#                             key=lambda v: v.get("totalNodes", 0), reverse=True)[:6]:
#         print(f"    {violation['id']:<30} {violation['impact']:<10} {violation['totalNodes']:>4} nodes")
#     return 0


# if __name__ == "__main__":
#     sys.exit(main())


#!/usr/bin/env python3
"""
Vet a candidate site BEFORE you build your demo around it.

    python scripts/check_target.py https://example.ac.in/

Checks, in order:
  1. Does robots.txt allow us?
  2. Does it respond at all, and how slowly?
  3. What does the mirrored copy score?  (30-55 is the sweet spot for a demo:
     bad enough to be worth fixing, not so broken it looks staged)
"""
import sys
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time
import httpx
from audit.mirror import USER_AGENT, mirror
from audit.runner import audit_html
from audit.scorer import score_from_violations


def robots_allows(url: str) -> bool:
    robots_url = urljoin(url, "/robots.txt")
    parser = RobotFileParser()
    try:
        response = httpx.get(robots_url, timeout=10, headers={"User-Agent": USER_AGENT})
        parser.parse(response.text.splitlines())
    except Exception as error:
        print(f"  robots.txt unreadable ({error}); treating as allowed")
        return True
    return parser.can_fetch(USER_AGENT, url)


def main() -> int:
    url = sys.argv[1]
    print(f"checking {url}")

    if not robots_allows(url):
        print("  BLOCKED by robots.txt -- pick another target")
        return 1
    print("  robots.txt: allowed")

    started = time.time()
    mirrored = mirror(url)
    print(f"  fetched in {time.time() - started:.1f}s [{mirrored.status_code}] "
          f"{len(mirrored.html)} bytes")
    for note in mirrored.notes:
        print(f"    {note}")

    result = audit_html(mirrored.html, take_screenshot=False)
    score = score_from_violations(result.violations)
    print(f"  score: {score}/100, {len(result.violations)} rules failing")

    if score > 85:
        print("  -> too accessible already, boring demo")
    elif score < 20:
        print("  -> extremely broken; good impact, but check the mirror renders correctly")
    else:
        print("  -> GOOD DEMO CANDIDATE")

    print("  top rules:")
    for violation in sorted(result.violations,
                            key=lambda v: v.get("totalNodes", 0), reverse=True)[:6]:
        print(f"    {violation['id']:<30} {violation['impact']:<10} {violation['totalNodes']:>4} nodes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
