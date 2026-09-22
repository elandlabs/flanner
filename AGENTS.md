<!-- flanner:managed -->
## Plan files (managed by flanner)

Design, architecture, and planning markdown for this repo is managed by flanner and lives in `.plans/` (project: flanner).

When the user asks to save a plan/design/architecture doc, confirm it is a plan, then use the flanner MCP tools instead of writing the file directly:

- `get_plan_config` to confirm the location and header format
- `create_plan_file_tool(project_id, name, content)` to create it (adds the YAML header and versions it)
- `update_plan_file_tool(plan_file_id, content)` to revise it

Never hand-write the YAML header; the tools generate it.
<!-- /flanner:managed -->

## Keep flanner-meshlab in step

`flanner-meshlab` (next to this checkout) tests Flanner Mesh end to end in a
Docker lab: two devices behind NAT routers, a control plane and a relay,
with no internet. Features that cross devices or the control plane change
what it has to prove.

When a change adds or alters a peer operation, sync, the roster or
entitlement, enrolment, messaging, or anything else that travels between
devices or through the control plane:

- Check whether a meshlab scenario covers it. If not, add or update one on
  a meshlab branch named like the feature's branch.
- Run it against this branch before merging:
  `meshlab test <scenario> --flanner <client checkout> --cloud <cloud checkout>`.
- Say in the PR which scenario covers the change, or why none is needed.

A lab that lags behind the product passes while real teams fail.
