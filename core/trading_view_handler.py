def trading_view_handle_func(data):
    print(data)
    val = data["name"]
    return {"msg" : "done"}