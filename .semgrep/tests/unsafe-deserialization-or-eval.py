import ast
import pickle as pickles
import yaml
from marshal import loads as load_code


def load_pickle(payload: bytes) -> object:
    # ruleid: unsafe-deserialization-or-eval
    return pickles.loads(payload)


def load_marshal(payload: bytes) -> object:
    # ruleid: unsafe-deserialization-or-eval
    return load_code(payload)


def unsafe_yaml_loader(payload: str) -> object:
    # ruleid: unsafe-deserialization-or-eval
    return yaml.load(payload, Loader=yaml.Loader)


def unsafe_yaml_extra_args(payload: str) -> object:
    options = {"version": yaml.Loader}
    # ruleid: unsafe-deserialization-or-eval
    return yaml.load(payload, **options)


def dynamic_code(source: str) -> object:
    # ruleid: unsafe-deserialization-or-eval
    return eval(source)


def dynamic_statement(source: str, scope: dict[str, object]) -> None:
    # ruleid: unsafe-deserialization-or-eval
    exec(source, scope)


def safe_values(payload: str) -> object:
    # ok: unsafe-deserialization-or-eval
    return yaml.safe_load(payload)
    # ok: unsafe-deserialization-or-eval
    return yaml.load(payload, Loader=yaml.SafeLoader)
    # ok: unsafe-deserialization-or-eval
    return ast.literal_eval(payload)
