from jef import mdtable


def test_round_trip_keeps_text_around_the_table():
    text = "# Title\n\nSome notes.\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\nTrailing note.\n"
    doc = mdtable.parse(text)
    assert doc.before == ["# Title", "", "Some notes.", ""]
    assert doc.headers == ["A", "B"]
    assert doc.rows == [["1", "2"]]
    assert doc.after == ["", "Trailing note."]
    assert mdtable.render(doc) == text


def test_escaped_pipes_survive_a_round_trip():
    doc = mdtable.TableDoc(headers=["A"], rows=[["x | y"]])
    text = mdtable.render(doc)
    assert "x \\| y" in text
    assert mdtable.parse(text).rows == [["x | y"]]


def test_short_rows_are_padded_to_header_width():
    doc = mdtable.parse("| A | B | C |\n| --- | --- | --- |\n| 1 |\n")
    assert doc.rows == [["1", "", ""]]


def test_document_without_table():
    doc = mdtable.parse("# Just a title\n")
    assert doc.headers == []
    assert doc.before == ["# Just a title"]


def test_newlines_in_cells_are_flattened():
    doc = mdtable.TableDoc(headers=["A"], rows=[["line one\nline two"]])
    assert "| line one line two |" in mdtable.render(doc)
