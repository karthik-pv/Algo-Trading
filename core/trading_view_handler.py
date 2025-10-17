from loguru import logger

def trading_view_handle_func(data):
    logger.debug(f"Received data from TradingView: {data}")
    val = data["name"]
    return {"msg" : "done"}