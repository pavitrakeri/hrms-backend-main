from google.genai import types

def get_tool_declarations():
    """Returns the list of Gemini Tool definitions for Aimploy HRMS."""
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="get_my_leave_balance",
                    description="Get the authenticated user's current leave balances (annual, sick, unpaid leaves).",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={},
                    ),
                ),
                types.FunctionDeclaration(
                    name="get_my_attendance_today",
                    description="Get the authenticated user's attendance status for today, including clock-in, clock-out, total hours, and location.",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={},
                    ),
                ),
                types.FunctionDeclaration(
                    name="get_my_tasks",
                    description="Get the list of active tasks assigned to the authenticated user.",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={
                            "status": types.Schema(
                                type="STRING",
                                description="Filter tasks by status: 'todo', 'in_progress', 'done', or leave empty for all."
                            )
                        },
                    ),
                ),
                types.FunctionDeclaration(
                    name="get_upcoming_holidays",
                    description="Get the list of upcoming official company and national holidays.",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={},
                    ),
                ),
                types.FunctionDeclaration(
                    name="search_company_policies",
                    description="Semantic search across Aimploy official policies, guidelines, codes of conduct, and leave rules.",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={
                            "query": types.Schema(
                                type="STRING",
                                description="The search term or policy question to look up."
                            )
                        },
                        required=["query"]
                    ),
                ),
                types.FunctionDeclaration(
                    name="propose_leave_application",
                    description="Propose a leave application draft for the user to review and confirm before submitting.",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={
                            "leave_type": types.Schema(
                                type="STRING",
                                description="Type of leave: 'annual', 'sick', or 'unpaid'."
                            ),
                            "start_date": types.Schema(
                                type="STRING",
                                description="Leave start date in YYYY-MM-DD format."
                            ),
                            "end_date": types.Schema(
                                type="STRING",
                                description="Leave end date in YYYY-MM-DD format."
                            ),
                            "reason": types.Schema(
                                type="STRING",
                                description="Reason for the leave request."
                            ),
                            "half_day": types.Schema(
                                type="BOOLEAN",
                                description="Whether this is a half day leave request."
                            )
                        },
                        required=["leave_type", "start_date", "end_date"]
                    ),
                ),
            ]
        )
    ]
