from scripts.live_test_1rub import make_token


def test_live_test_token_matches_tbank_docs_vector():
    params = {
        "TerminalKey": "MerchantTerminalKey",
        "Amount": 19200,
        "OrderId": "00000",
        "Description": "Подарочная карта на 1000 рублей",
    }

    assert (
        make_token(params, "11111111111111")
        == "72dd466f8ace0a37a1f740ce5fb78101712bc0665d91a8108c7c8a0ccd426db2"
    )
