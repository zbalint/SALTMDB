from saltmdb.ephemeral_store import EphemeralStore

ephemeral_store = EphemeralStore()


def main():
    print("saltmdb main func")

    res=ephemeral_store.store("test_key", "test_val")
    print(res)
    emem = ephemeral_store.get("test_key")
    print(emem)

if __name__ == "__main__":
    main()