"""Business services: the ONLY code that reads or writes business data
(documents' text, leave, access requests, payslips, IT tickets, the
directory). Called identically by the MCP tools (agent path) and by the
REST routers (frontend path) - so a permission check lives in exactly one
place and cannot be bypassed by taking the other road. Every function takes
the authenticated User and their effective Grant resolved server-side;
none ever accepts a user_id, role or department from the caller as a
source of authority.
"""
