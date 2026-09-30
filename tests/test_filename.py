import pytest

from rassi.filename import FilenameError, normalize_period, parse_source_filename


def test_parses_standard_filename():
    info = parse_source_filename("data/in/020421/020421_PD1_RASSI.pdf")
    assert info.teacher_code == "020421"
    assert info.period == "1"
    assert info.form == "RASSI"
    assert info.source_pdf == "020421_PD1_RASSI.pdf"


def test_keeps_leading_zeros_in_teacher_code_and_multi_digit_period():
    info = parse_source_filename("000123_PD12_RASSI.pdf")
    assert info.teacher_code == "000123"
    assert info.period == "12"


@pytest.mark.parametrize(
    "name",
    [
        "020421_PD1_RASSI.PDF",
        "020421_P1_RASSI.pdf",
        "020421_PD_RASSI.pdf",
        "020421_PD1_RASS.pdf",
        "020421-PD1-RASSI.pdf",
        "020421_PD1_RASSI_scan.pdf",
        "PD1_RASSI.pdf",
        "020421_PD1_RASSI.png",
    ],
)
def test_rejects_non_matching_names(name):
    with pytest.raises(FilenameError) as error:
        parse_source_filename(name)
    assert name in str(error.value)
    assert "020421_PD1_RASSI.pdf" in str(error.value)


@pytest.mark.parametrize("raw, expected", [("1", "1"), ("01", "1"), ("PD1", "1"), (" 3 ", "3"), (None, None)])
def test_normalize_period(raw, expected):
    assert normalize_period(raw) == expected
