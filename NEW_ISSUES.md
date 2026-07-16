There some you issues. I started using SALTMDB at my work.
So from now on we still release alpha verstion but treat it like a prod tool, so you should always provide a migration doc when you modify the db schemas.

These are coming from the agent that uses the SALTMDB now actively as its prod memory.
You can push back on any issue if you disagree with it or you can implemnt a different way to achive the same outcome.

1. Missing event-read path. The agent cant see the consolidation requests from the Librarian, beacuse it cant query the event log

2. Duplicate memory risk: store_knowledge is easy to append repeatedly. Add lightweight dedup/upsert policy for example title!tag hash pr required entity_id on updates

3. Tag drift control: cannonical tasg exists, but new memiries can still fragment taxnonomy over time. Enforce pre-write tag normalization via get_canonical_tags

4. Retrival precision by owner/scope: keep owner_id % scope mandatory in write and read wrappers to prevent accidental cross-lane signals

5. Consolidation hígine: schedule regular consolidation of overlapping "runbook/decision" memories so retrieval stays high-signal

