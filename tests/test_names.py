from pixel.ui.names import from_address, from_signature


def test_company_person_and_town_from_the_signature():
    body = "Hello,\n\nplease quote 300 kg FRA-JFK.\n\nCheers,\nKlaus Weidner\nWeidner Spielwaren GmbH, Nuremberg"
    assert from_signature(body, "weidner-spielwaren@x.mail") == (
        "Weidner Spielwaren GmbH",
        "Klaus Weidner",
        "Nuremberg",
    )


def test_a_role_line_is_not_the_person():
    body = "Thanks\n\nTobias Auer\nVersand / Shipping\nAuer Maschinenbau GmbH"
    assert from_signature(body, "auer-maschinenbau@x.mail") == ("Auer Maschinenbau GmbH", "Tobias Auer", "")


def test_a_company_without_legal_form_matches_its_address():
    assert from_signature("Hi\n\nSara\nLindqvist Medtech", "lindqvist-medtech@x.mail") == (
        "Lindqvist Medtech",
        "Sara",
        "",
    )


def test_quoted_history_and_data_lines_are_not_a_signature():
    body = "Where is it?\n\nLindqvist Medtech GmbH | 51447 to ORD | 676 kg\n\nOn Monday Petra wrote:\n> Meyer GmbH"
    assert from_signature(body, "tracking@x.mail") is None


def test_the_address_when_no_signature_names_the_company():
    assert from_address("lindqvist-medtech@hackloop.mail") == "Lindqvist Medtech"
    assert from_address("buyer@acme-gmbh.hackloop.mail") == "Acme GmbH"
