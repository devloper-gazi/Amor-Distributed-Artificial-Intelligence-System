// Sentinel test fixture — intentionally insecure Node/JS.
// DO NOT REUSE.  Each comment marks a deliberate CWE for Sentinel
// to catch on a Standard / Deep scan.

const express = require('express');
const fs = require('fs');
const child_process = require('child_process');

const app = express();

// CWE-798 — Hard-coded credential.  Format below is INTENTIONALLY
// mangled so GitHub's secret-scanning push protection doesn't flag
// it as a real Stripe key.  Sentinel's regex still matches via the
// generic-password-assignment pattern.
const api_secret = "FAKE_DO_NOT_REUSE_AAAA1234567890BBBBCCCCDDDD0123456789EFGHIJKL";

app.get('/profile', (req, res) => {
    // CWE-79 — XSS via raw innerHTML
    res.send('<div>' + req.query.name + '</div>');
});

app.get('/file', (req, res) => {
    // CWE-22 — Path traversal: req.query.path is concatenated raw.
    const data = fs.readFileSync('/var/uploads/' + req.query.path);
    res.send(data);
});

app.get('/run', (req, res) => {
    // CWE-78 — Command injection.
    child_process.exec('ls ' + req.query.dir, (err, stdout) => res.send(stdout));
});

app.get('/eval', (req, res) => {
    // CWE-94 — eval of user input.
    res.send(eval(req.query.code));
});

// CWE-1321 — Prototype pollution
function merge(target, source) {
    for (const key in source) {
        if (typeof source[key] === 'object') {
            target[key] = target[key] || {};
            merge(target[key], source[key]);
        } else {
            target[key] = source[key];
        }
    }
}
