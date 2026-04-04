from functools import wraps

from app.utils.metrics import monitor_api


def monitor(name: str):
    decorator = monitor_api(name)

    def outer(func):
        wrapped = decorator(func)

        @wraps(func)
        async def inner(*args, **kwargs):
            return await wrapped(*args, **kwargs)

        return inner

    return outer
