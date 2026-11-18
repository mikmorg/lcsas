/*
 * test_path.c -- path-traversal safety.  Mirrors
 * tests/unit/test_restic_fallback_path_traversal.py.
 */
#include "path.h"
#include <stdio.h>
#include <string.h>

static int fails = 0;

static void
expect_safe(const char *label, const char *name, int want_ok)
{
    int rc = lcsas_path_safe_name(name);
    int got_ok = (rc == 0);
    if (got_ok != want_ok) {
        fprintf(stderr, "FAIL %s name='%s' rc=%d (want %s)\n",
                label, name, rc, want_ok ? "OK" : "REJECT");
        fails++;
    }
}

static void
expect_win(const char *label, const char *name, int want_ok)
{
    int rc = lcsas_path_safe_name_windows(name);
    int got_ok = (rc == 0);
    if (got_ok != want_ok) {
        fprintf(stderr, "FAIL %s name='%s' rc=%d (want %s)\n",
                label, name, rc, want_ok ? "OK" : "REJECT");
        fails++;
    }
}

static void
expect_sym(const char *label, const char *root, const char *from,
           const char *target, int want_ok)
{
    int rc = lcsas_path_safe_symlink(root, from, target);
    int got_ok = (rc == 0);
    if (got_ok != want_ok) {
        fprintf(stderr, "FAIL %s root=%s from=%s tgt=%s rc=%d (want %s)\n",
                label, root, from, target, rc, want_ok ? "OK" : "REJECT");
        fails++;
    }
}

/* Issue #781: `in` (escaped text, in_len bytes) must decode to `want`
 * (want_len raw bytes); want_len < 0 means "must be refused". */
static void
expect_unescape(const char *label, const char *in, size_t in_len,
                const char *want, long want_len)
{
    char out[64];
    long n = lcsas_unescape_name(in, in_len, out, sizeof out);
    if (want_len < 0) {
        if (n >= 0) {
            fprintf(stderr, "FAIL unescape %s: decoded (len %ld), want "
                    "refusal\n", label, n);
            fails++;
        }
        return;
    }
    if (n != want_len || memcmp(out, want, (size_t)want_len) != 0
            || out[n] != '\0') {
        fprintf(stderr, "FAIL unescape %s: got len %ld\n", label, n);
        fails++;
    }
}

#define UNESC(label, in, want) \
    expect_unescape(label, in, sizeof(in) - 1, want, (long)sizeof(want) - 1)
#define UNESC_BAD(label, in) \
    expect_unescape(label, in, sizeof(in) - 1, "", -1)

static void
expect_reason(const char *label, const char *got, int want_refused)
{
    if ((got != NULL) != want_refused) {
        fprintf(stderr, "FAIL %s: %s\n", label,
                got ? got : "accepted, want refused");
        fails++;
    }
}

static void
test_unescape(void)
{
    char small[4];

    /* Plain names (and valid UTF-8) are copied as they are. */
    UNESC("plain",      "plain.txt",          "plain.txt");
    UNESC("utf8",       "caf\xc3\xa9",        "caf\xc3\xa9");
    UNESC("empty",      "",                   "");
    /* The vectors rustic 0.11.2 wrote for the issue #781 names. */
    UNESC("quote",      "quo\\\"te.txt",       "quo\"te.txt");
    UNESC("backslash",  "back\\\\slash.txt",  "back\\slash.txt");
    UNESC("tab",        "tab\\there.txt",     "tab\there.txt");
    UNESC("latin1",     "latin\\xe9.txt",     "latin\xe9.txt");
    UNESC("lone lead",  "lone-\\xc3",         "lone-\xc3");
    UNESC("surrogate bytes", "s\\xed\\xa0\\x80x", "s\xed\xa0\x80x");
    UNESC("controls",   "\\a\\b\\f\\n\\r\\t\\v", "\a\b\f\n\r\t\v");
    UNESC("quotes",     "\\'\\`",             "'`");
    UNESC("hex upper",  "\\xAB\\xff",         "\xab\xff");
    UNESC("hex nul",    "a\\x00b",            "a\0b");
    UNESC("hex slash",  "\\x2f",              "/");
    /* The vectors restic 0.19 (Go strconv.Quote) writes. */
    UNESC("go esc",     "esc\\x1bx",          "esc\x1bx");
    UNESC("go del",     "del\\x7fx",          "del\x7fx");
    UNESC("go u",       "zw-\\u200bx",        "zw-\xe2\x80\x8bx");
    UNESC("go u 2byte", "c1-\\u0085x",        "c1-\xc2\x85x");
    UNESC("go u ascii", "\\u0041",            "A");
    UNESC("go U",       "max-\\U0010ffffx",   "max-\xf4\x8f\xbf\xbfx");
    UNESC("go U emoji", "\\U0001F600",        "\xf0\x9f\x98\x80");
    UNESC("go u FFFF",  "\\uffff",            "\xef\xbf\xbf");

    /* Text no encoder writes: refused, never used literally. */
    UNESC_BAD("trailing backslash", "abc\\");
    UNESC_BAD("unknown escape",     "back\\slash");
    UNESC_BAD("octal",              "\\101");
    UNESC_BAD("short hex",          "\\x4");
    UNESC_BAD("non-hex",            "\\xg0");
    UNESC_BAD("plus sign",          "\\x+f");
    UNESC_BAD("short u",            "\\u12");
    UNESC_BAD("surrogate u",        "\\ud800");
    UNESC_BAD("surrogate U",        "\\U0000DFFF");
    UNESC_BAD("above max",          "\\U00110000");
    UNESC_BAD("huge U",             "\\UFFFFFFFF");

    /* Output buffer too small (the NUL must fit too). */
    if (lcsas_unescape_name("abcd", 4, small, sizeof small) >= 0) {
        fprintf(stderr, "FAIL unescape overflow: accepted\n");
        fails++;
    }
    if (lcsas_unescape_name("abc", 3, small, sizeof small) != 3) {
        fprintf(stderr, "FAIL unescape exact fit: refused\n");
        fails++;
    }
    if (lcsas_unescape_name("\\U0001F600", 10, small, sizeof small) >= 0) {
        fprintf(stderr, "FAIL unescape 4-byte overflow: accepted\n");
        fails++;
    }
    if (lcsas_unescape_name("a", 1, small, 0) >= 0
            || lcsas_unescape_name(NULL, 0, small, sizeof small) >= 0) {
        fprintf(stderr, "FAIL unescape bad args: accepted\n");
        fails++;
    }

    /* Path safety on the DECODED bytes. */
    expect_reason("ok",       lcsas_name_refusal("a.txt", 5), 0);
    expect_reason("dots ok",  lcsas_name_refusal("...", 3), 0);
    expect_reason("empty",    lcsas_name_refusal("", 0), 1);
    expect_reason("dot",      lcsas_name_refusal(".", 1), 1);
    expect_reason("dotdot",   lcsas_name_refusal("..", 2), 1);
    expect_reason("slash",    lcsas_name_refusal("a/b", 3), 1);
    expect_reason("nul",      lcsas_name_refusal("a\0b", 3), 1);

    /* The Windows rule. */
    expect_reason("win ok",   lcsas_name_windows_refusal("a b.txt", 7), 0);
    expect_reason("win utf8", lcsas_name_windows_refusal(
                      "\xc3\xa9\xe2\x80\x8b\xf0\x9f\x98\x80\xf4\x8f\xbf\xbf",
                      13), 0);
    expect_reason("win bs",   lcsas_name_windows_refusal("a\\b", 3), 1);
    expect_reason("win colon",lcsas_name_windows_refusal("a:b", 3), 1);
    expect_reason("win star", lcsas_name_windows_refusal("a*", 2), 1);
    expect_reason("win q",    lcsas_name_windows_refusal("a?", 2), 1);
    expect_reason("win dq",   lcsas_name_windows_refusal("a\"", 2), 1);
    expect_reason("win lt",   lcsas_name_windows_refusal("<", 1), 1);
    expect_reason("win gt",   lcsas_name_windows_refusal(">", 1), 1);
    expect_reason("win pipe", lcsas_name_windows_refusal("|", 1), 1);
    expect_reason("win tab",  lcsas_name_windows_refusal("a\tb", 3), 1);
    expect_reason("win nul",  lcsas_name_windows_refusal("a\0", 2), 1);
    expect_reason("win latin1",   lcsas_name_windows_refusal("\xe9", 1), 1);
    expect_reason("win cont",     lcsas_name_windows_refusal("\x80", 1), 1);
    expect_reason("win overlong2",lcsas_name_windows_refusal("\xc0\xaf", 2), 1);
    expect_reason("win overlong3",lcsas_name_windows_refusal("\xe0\x80\xaf", 3), 1);
    expect_reason("win overlong4",
                  lcsas_name_windows_refusal("\xf0\x80\x80\xaf", 4), 1);
    expect_reason("win surrogate",lcsas_name_windows_refusal("\xed\xa0\x80", 3), 1);
    expect_reason("win above max",lcsas_name_windows_refusal("\xf4\x90\x80\x80", 4), 1);
    expect_reason("win f5",       lcsas_name_windows_refusal("\xf5\x80\x80\x80", 4), 1);
    expect_reason("win short",    lcsas_name_windows_refusal("\xe2\x80", 2), 1);
    expect_reason("win bad 3rd",  lcsas_name_windows_refusal("\xe2\x80\x41", 3), 1);
    /* Issue #619: a trailing dot or space, a device name (alone or with
     * an extension, any case, COM/LPT with a superscript digit), and a
     * name longer than 255 UTF-16 units. */
    expect_reason("win dot end",  lcsas_name_windows_refusal("trail.", 6), 1);
    expect_reason("win sp end",   lcsas_name_windows_refusal("trail ", 6), 1);
    expect_reason("win dot mid",  lcsas_name_windows_refusal(".hidden", 7), 0);
    expect_reason("win CON",      lcsas_name_windows_refusal("CON", 3), 1);
    expect_reason("win con.txt",  lcsas_name_windows_refusal("con.txt", 7), 1);
    expect_reason("win Nul.a.b",  lcsas_name_windows_refusal("Nul.a.b", 7), 1);
    expect_reason("win prn sp",   lcsas_name_windows_refusal("prn .x", 6), 1);
    expect_reason("win aux",      lcsas_name_windows_refusal("aux", 3), 1);
    expect_reason("win COM1",     lcsas_name_windows_refusal("COM1", 4), 1);
    expect_reason("win lpt9.x",   lcsas_name_windows_refusal("lpt9.x", 6), 1);
    expect_reason("win COM sup1", lcsas_name_windows_refusal("COM\xc2\xb9", 5), 1);
    expect_reason("win LPT sup2", lcsas_name_windows_refusal("lpt\xc2\xb2.t", 7), 1);
    expect_reason("win COM sup3", lcsas_name_windows_refusal("Com\xc2\xb3", 5), 1);
    expect_reason("win COM sup4", lcsas_name_windows_refusal("COM\xc2\xb4", 5), 0);
    expect_reason("win COM a0",   lcsas_name_windows_refusal("COM\xc2\xa0", 5), 0);
    expect_reason("win COM0",     lcsas_name_windows_refusal("COM0", 4), 0);
    expect_reason("win COMX",     lcsas_name_windows_refusal("COMX", 4), 0);
    expect_reason("win LPT",      lcsas_name_windows_refusal("LPT", 3), 0);
    expect_reason("win CONS",     lcsas_name_windows_refusal("CONSOLE", 7), 0);
    expect_reason("win CO",       lcsas_name_windows_refusal("CO", 2), 0);
    expect_reason("win xCON",     lcsas_name_windows_refusal("xCON", 4), 0);
    expect_reason("win CON1x",    lcsas_name_windows_refusal("COM12", 5), 0);
    expect_reason("win AUXx",     lcsas_name_windows_refusal("AUXx.txt", 8), 0);
    expect_reason("win my con",   lcsas_name_windows_refusal("my con.txt", 10), 0);
    {
        /* 255 units: fits; 256: refused.  A 4-byte UTF-8 sequence (an
         * emoji) counts as two units, a 3-byte one as one. */
        char longname[600];
        memset(longname, 'a', 256);
        expect_reason("win 255",  lcsas_name_windows_refusal(longname, 255), 0);
        expect_reason("win 256",  lcsas_name_windows_refusal(longname, 256), 1);
        memset(longname, 0, sizeof longname);
        {
            size_t k;
            for (k = 0; k < 127; k++) memcpy(longname + 4 * k, "\xf0\x9f\x98\x80", 4);
            longname[508] = 'a';
            expect_reason("win 4b 255",
                          lcsas_name_windows_refusal(longname, 509), 0);
            longname[509] = 'b';
            expect_reason("win 4b 256",
                          lcsas_name_windows_refusal(longname, 510), 1);
            for (k = 0; k < 85; k++) memcpy(longname + 3 * k, "\xe2\x82\xac", 3);
            expect_reason("win 3b 85",
                          lcsas_name_windows_refusal(longname, 255), 0);
        }
    }

    /* The UTF-8 test the macOS build applies. */
    if (!lcsas_name_is_utf8("caf\xc3\xa9", 5) || !lcsas_name_is_utf8("", 0)
            || lcsas_name_is_utf8("latin\xe9", 6)
            || lcsas_name_is_utf8("\xed\xa0\x80", 3)) {
        fprintf(stderr, "FAIL lcsas_name_is_utf8\n");
        fails++;
    }
}

int main(void)
{
    test_unescape();

    expect_safe("plain",         "foo/bar",       1);
    expect_safe("deep",          "a/b/c/d.txt",   1);
    expect_safe("dotdot",        "foo/../bar",    0);
    expect_safe("dotdot-start",  "../bar",        0);
    expect_safe("absolute",      "/etc/passwd",   0);
    expect_safe("empty",         "",              0);
    expect_safe("trailing-slash","foo//bar",      0);
    expect_safe("dot-only",      ".",             1);
    /* Issue #195 — pin verbatim cases cited in the issue text so
     * regressions surface against the exact strings reviewers expect
     * the safety check to reject. */
    expect_safe("dotdot-passwd", "../etc/passwd", 0);
    expect_safe("abs-bare",      "/abs",          0);
    expect_safe("dotdot-trail",  "foo/..",        0);

    /* Symlinks */
    expect_sym("relative inside", "/restore", "/restore/a", "b",        1);
    expect_sym("relative escape", "/restore", "/restore/a", "../../etc/passwd", 0);
    /* Absolute targets allowed (issue #187 — user decision to match
     * tier-2 / rustic behaviour).  See path.c comment for the
     * containment-property tradeoff.  FORMAT.txt PATH SAFETY documents
     * this as "restored as-is ... containment is NOT guaranteed"
     * (reconciled in T1C-03 — the doc previously claimed rejection). */
    expect_sym("absolute",        "/restore", "/restore/a", "/etc/passwd",      1);
    expect_sym("dotdot to root",  "/restore", "/restore/a", "..",       1);  /* lands at /restore */
    expect_sym("dotdot past root","/restore", "/restore",   "..",       0);

    /* Issue #619: the Windows rule for a relative path. */
    expect_win("win plain",      "foo\\bar/baz",  1);
    expect_win("win dotdot bs",  "..\\evil",      0);
    expect_win("win dotdot mid", "a\\..\\b",     0);
    expect_win("win dotdot end", "a\\..",         0);
    expect_win("win lead bs",    "\\abs",         0);
    expect_win("win empty seg",  "a\\\\b",       0);
    expect_win("win mixed empty","a/\\b",         0);
    expect_win("win drive",      "C:evil",        0);
    expect_win("win drive abs",  "C:\\x",        0);
    expect_win("win stream",     "a\\b:stream",  0);
    expect_win("win dotdot fs",  "a/../b",        0);
    expect_win("win abs",        "/abs",          0);
    expect_win("win empty",      "",              0);
    expect_win("win dots ok",    "...\\a..b",    1);
    /* The POSIX rule keeps '\' as a name byte. */
#ifndef _WIN32
    expect_safe("posix bs",      "..\\x",        1);
    expect_safe("posix colon",   "C:x",           1);
#endif

    if (fails == 0) printf("test_path: OK\n");
    return fails ? 1 : 0;
}
