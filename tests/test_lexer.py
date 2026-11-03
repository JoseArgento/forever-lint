from forever_lint.lexer import COMMENT, KEYWORD, NAME, NUMBER, OP, STRING, tokenize
from forever_lint.luatable import parse_tables


def kinds(src):
    return [(t.kind, t.value) for t in tokenize(src)]


def test_comments_and_strings_are_not_code():
    src = '-- GetSpellInfo()\n--[[ GetItemInfo() ]] local s = "UnitAura()" .. [[GetMouseFocus]] .. [==[ x ]] y ]==]'
    names = [v for k, v in kinds(src) if k == NAME]
    assert names == ["s"]


def test_long_comment_with_equals_and_lines():
    toks = tokenize("--[==[\n a ]] b \n]==]\nx = 1", keep_comments=True)
    assert toks[0].kind == COMMENT
    x = next(t for t in toks if t.value == "x")
    assert x.line == 4 and x.col == 1


def test_numbers_operators_keywords():
    assert kinds("a = 0x1F + 1.5e3 .. b ~= c ... ::lbl::") == [
        (NAME, "a"),
        (OP, "="),
        (NUMBER, "0x1F"),
        (OP, "+"),
        (NUMBER, "1.5e3"),
        (OP, ".."),
        (NAME, "b"),
        (OP, "~="),
        (NAME, "c"),
        (OP, "..."),
        (OP, "::"),
        (NAME, "lbl"),
        (OP, "::"),
    ]
    assert kinds("local function f() end")[0] == (KEYWORD, "local")


def test_escapes_and_unterminated_string():
    toks = tokenize("x = 'it\\'s' y = \"\\65\\n\" z = 'open")
    strings = [t.value for t in toks if t.kind == STRING]
    assert strings[0] == "it's" and strings[1] == "A\n" and strings[2] == "open"


def test_positions():
    toks = tokenize("a\n  b = c")
    b = next(t for t in toks if t.value == "b")
    assert (b.line, b.col) == (2, 3)
    assert toks[-1].pos == len("a\n  b = ")


def test_luatable_reads_documentation_shape():
    src = """
local Doc = {
    Name = "Spell", Type = "System", Namespace = "C_Spell",
    Functions = {
        { Name = "GetSpellInfo", Type = "Function", SecretReturns = true,
          Returns = { { Name = "info", Type = "SpellInfo", Nilable = false, Default = Enum.Foo.Bar } } },
    },
    Tables = { { Name = "Weird", Value = 1 + 2, Other = -3 } },
};
APIDocumentation:AddDocumentationTable(Doc);
"""
    doc = parse_tables(src)["Doc"]
    fn = doc["Functions"][0]
    assert doc["Namespace"] == "C_Spell" and fn["SecretReturns"] is True
    assert fn["Returns"][0]["Default"] == "Enum.Foo.Bar"
    assert doc["Tables"][0]["Value"] is None and doc["Tables"][0]["Other"] == -3
