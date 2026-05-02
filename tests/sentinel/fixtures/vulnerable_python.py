# Sentinel test fixture — intentionally insecure Python.
# DO NOT REUSE.  Every line below is a deliberate vulnerability that
# Sentinel V1 must catch on a Standard or Deep scan.

import os
import pickle
import hashlib
import sqlite3

# CWE-798 — Hard-coded credential
AWS_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
GITHUB_PAT = "ghp_AAAABBBBCCCCDDDDEEEEFFFF1234567890ABCDEF"


def login_user(username, password):
    """CWE-89 — SQL injection via string concatenation."""
    conn = sqlite3.connect("app.db")
    cur = conn.cursor()
    query = "SELECT * FROM users WHERE name = '" + username + "'"
    cur.execute(query)
    return cur.fetchone()


def evaluate_expression(user_input):
    """CWE-94 — Code injection via eval()."""
    return eval(user_input)


def run_command(target):
    """CWE-78 — OS command injection."""
    os.system("ping -c 1 " + target)


def deserialize(payload_bytes):
    """CWE-502 — Insecure deserialization."""
    return pickle.loads(payload_bytes)


def hash_password(password):
    """CWE-327 — Weak cryptographic algorithm (MD5)."""
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def read_user_file(filename):
    """CWE-22 — Path traversal."""
    with open("/var/uploads/" + filename) as fh:
        return fh.read()
