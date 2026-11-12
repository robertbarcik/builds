"""Give the `guest` login a fresh, easy-to-dictate password (word-word-1234).

Rewrites (or adds) the single APP_PASSWORD line in the repo's .env (gitignored), keeps the
previous file as .env.bak (gitignored) and prints the new password. Called by deploy/launch.sh;
on a running box copy the .env over and restart the service afterwards (the cookie signing key
derives from the passwords, so everyone is logged out). Copied from localdesk.
"""
import pathlib
import secrets
import shutil

ENV = pathlib.Path(__file__).resolve().parent.parent / ".env"
WORDS = ["orbit", "maple", "river", "amber", "cedar", "delta", "ember", "falcon", "harbor", "lumen",
         "meadow", "nectar", "onyx", "pixel", "quartz", "saffron", "tundra", "velvet", "willow", "zephyr"]

text = ENV.read_text()
lines = text.splitlines()
hits = [i for i, line in enumerate(lines) if line.startswith("APP_PASSWORD=")]
assert len(hits) <= 1, f"expected at most one APP_PASSWORD line in {ENV}, found {len(hits)}"

new = f"{secrets.choice(WORDS)}-{secrets.choice(WORDS)}-{secrets.randbelow(9000) + 1000}"
shutil.copy2(ENV, ENV.with_name(".env.bak"))
if hits:
    lines[hits[0]] = f"APP_PASSWORD={new}"
else:
    lines.append(f"APP_PASSWORD={new}")
ENV.write_text("\n".join(lines) + "\n")
print(new)
