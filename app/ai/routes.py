import json
import logging
import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from google.genai import types

from app.db import get_db_pool
from app.features.auth import get_current_user
from app.ai.config import get_gemini_client, GEMINI_MODEL, SYSTEM_INSTRUCTION
from app.ai.tools import get_tool_declarations
from app.ai.dispatcher import dispatch_tool_call
from app.ai.guardrails import sanitize_user_input
from app.ai.rag import index_all_policies
from app.features.leaves import apply_leave_with_upload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["AI Assistant"])

class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None

class ConfirmActionRequest(BaseModel):
    action_id: str
    confirm: bool

@router.post("/chat")
async def ai_chat(req: ChatRequest, user=Depends(get_current_user)):
    """
    Primary conversational endpoint with multi-turn tool calling and security checks.
    """
    clean_message = sanitize_user_input(req.message)
    db_pool = get_db_pool()

    async with db_pool.acquire() as conn:
        # 1. Resolve or create conversation
        conversation_id = req.conversation_id
        if conversation_id:
            conv = await conn.fetchrow(
                "SELECT id FROM ai_conversations WHERE id=$1 AND user_id=$2",
                conversation_id, user["id"]
            )
            if not conv:
                conversation_id = None

        if not conversation_id:
            title = clean_message[:40] + ("..." if len(clean_message) > 40 else "")
            conv = await conn.fetchrow("""
                INSERT INTO ai_conversations (id, user_id, title)
                VALUES (gen_random_uuid(), $1, $2)
                RETURNING id
            """, user["id"], title)
            conversation_id = str(conv["id"])

        # 2. Save user message to database
        await conn.execute("""
            INSERT INTO ai_messages (id, conversation_id, sender, content)
            VALUES (gen_random_uuid(), $1, 'user', $2)
        """, conversation_id, clean_message)

        # 3. Check if Gemini Client is initialized
        client = get_gemini_client()
        if not client:
            fallback_response = (
                "Hello! I am your Aimploy AI Assistant. To enable full generative AI and tool execution, "
                "please configure the `GEMINI_API_KEY` in the backend environment. "
                "In the meantime, you can manage your leaves, attendance, and tasks directly in the HRMS navigation menu."
            )
            await conn.execute("""
                INSERT INTO ai_messages (id, conversation_id, sender, content)
                VALUES (gen_random_uuid(), $1, 'assistant', $2)
            """, conversation_id, fallback_response)
            return {
                "conversation_id": conversation_id,
                "response": fallback_response,
                "action_card": None
            }

        # 4. Load recent message history for context
        recent_rows = await conn.fetch("""
            SELECT sender, content, tool_calls, tool_results
            FROM ai_messages
            WHERE conversation_id=$1
            ORDER BY created_at ASC
            LIMIT 10
        """, conversation_id)

        # Build contents for Gemini
        contents = []
        for r in recent_rows:
            role = "user" if r["sender"] == "user" else "model"
            contents.append(types.Content(
                role=role,
                parts=[types.Part.from_text(text=r["content"])]
            ))

        # 5. Call Gemini with Tool Calling config
        tools = get_tool_declarations()
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION + f"\nCurrent User: {user.get('email')} (Role: {user.get('role')}). Today is {date.today().isoformat()}.",
            tools=tools,
            temperature=0.2
        )

        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=config
            )
        except Exception as e:
            logger.error(f"Gemini API error: {e}", exc_info=True)
            err_msg = "I encountered an issue connecting to the AI service. Please try again in a moment."
            return {"conversation_id": conversation_id, "response": err_msg, "action_card": None}

        # 6. Check for Function / Tool Calls
        action_card = None
        assistant_text = ""
        tool_calls_record = []
        tool_results_record = []

        candidate = response.candidates[0] if response.candidates else None
        if candidate and candidate.content and candidate.content.parts:
            function_calls = [p.function_call for p in candidate.content.parts if p.function_call]
            
            if function_calls:
                # Execute tools
                tool_contents = list(contents)
                tool_contents.append(candidate.content)

                tool_response_parts = []
                for fc in function_calls:
                    fn_name = fc.name
                    fn_args = dict(fc.args) if fc.args else {}
                    tool_calls_record.append({"name": fn_name, "args": fn_args})

                    # Dispatch tool
                    result = await dispatch_tool_call(conn, fn_name, fn_args, user)
                    tool_results_record.append({"name": fn_name, "result": result})

                    # If this is a proposed action requiring confirmation, prepare action_card
                    if isinstance(result, dict) and result.get("action_required") == "confirmation":
                        action_card = result

                    tool_response_parts.append(
                        types.Part.from_function_response(
                            name=fn_name,
                            response={"result": result}
                        )
                    )

                tool_contents.append(types.Content(role="user", parts=tool_response_parts))

                # Second turn: Send tool results back to Gemini to synthesize final response
                second_response = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=tool_contents,
                    config=config
                )
                assistant_text = second_response.text or "I have processed your request."
            else:
                assistant_text = response.text or "How can I assist you with HRMS today?"
        else:
            assistant_text = response.text or "I am here to help with your HR needs."

        # 7. Save assistant message
        await conn.execute("""
            INSERT INTO ai_messages (id, conversation_id, sender, content, tool_calls, tool_results)
            VALUES (gen_random_uuid(), $1, 'assistant', $2, $3, $4)
        """, conversation_id, assistant_text, json.dumps(tool_calls_record), json.dumps(tool_results_record))

        return {
            "conversation_id": conversation_id,
            "response": assistant_text,
            "action_card": action_card
        }

@router.post("/confirm-action")
async def confirm_action(req: ConfirmActionRequest, bg: BackgroundTasks, user=Depends(get_current_user)):
    """
    Human-in-the-loop action confirmation endpoint.
    """
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        action = await conn.fetchrow("""
            SELECT id, user_id, tool_name, tool_args, status, user_confirmed
            FROM ai_audit_logs
            WHERE id=$1 AND user_id=$2
        """, req.action_id, user["id"])

        if not action:
            raise HTTPException(status_code=404, detail="Action not found or unauthorized")

        if action["status"] != "pending":
            return {"status": action["status"], "message": f"Action is already {action['status']}."}

        if not req.confirm:
            await conn.execute("""
                UPDATE ai_audit_logs
                SET status='cancelled', user_confirmed=false
                WHERE id=$1
            """, action["id"])
            return {"status": "cancelled", "message": "Action has been cancelled."}

        # User approved: Commit the action
        tool_name = action["tool_name"]
        tool_args = json.loads(action["tool_args"]) if isinstance(action["tool_args"], str) else action["tool_args"]

        if tool_name == "apply_leave":
            try:
                res = await apply_leave_with_upload(
                    conn,
                    user,
                    bg,
                    leave_type_id=int(tool_args["leave_type_id"]),
                    start_date=datetime.strptime(tool_args["start_date"], "%Y-%m-%d").date(),
                    end_date=datetime.strptime(tool_args["end_date"], "%Y-%m-%d").date(),
                    half_day=bool(tool_args.get("half_day", False)),
                    half_day_slot=None,
                    reason=tool_args.get("reason", "Applied via AI Assistant"),
                    file=None
                )

                await conn.execute("""
                    UPDATE ai_audit_logs
                    SET status='executed', user_confirmed=true, result=$1
                    WHERE id=$2
                """, json.dumps(res), action["id"])

                return {
                    "status": "success",
                    "message": "Leave application submitted successfully!",
                    "leave_id": res.get("leave_id")
                }
            except HTTPException as he:
                await conn.execute("""
                    UPDATE ai_audit_logs SET status='failed', result=$1 WHERE id=$2
                """, json.dumps({"error": he.detail}), action["id"])
                raise he
            except Exception as e:
                await conn.execute("""
                    UPDATE ai_audit_logs SET status='failed', result=$1 WHERE id=$2
                """, json.dumps({"error": str(e)}), action["id"])
                raise HTTPException(status_code=500, detail=f"Failed to submit leave: {str(e)}")

        return {"status": "unknown", "message": f"Unsupported action type: {tool_name}"}

@router.get("/conversations")
async def list_conversations(user=Depends(get_current_user)):
    """List recent conversations for user."""
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, title, created_at, updated_at
            FROM ai_conversations
            WHERE user_id=$1
            ORDER BY updated_at DESC
            LIMIT 20
        """, user["id"])
        return [
            {
                "id": str(r["id"]),
                "title": r["title"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None
            }
            for r in rows
        ]

@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: str, user=Depends(get_current_user)):
    """Fetch all messages for a specific conversation."""
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        conv = await conn.fetchrow(
            "SELECT id FROM ai_conversations WHERE id=$1 AND user_id=$2",
            conversation_id, user["id"]
        )
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")

        rows = await conn.fetch("""
            SELECT id, sender, content, tool_calls, tool_results, created_at
            FROM ai_messages
            WHERE conversation_id=$1
            ORDER BY created_at ASC
        """, conversation_id)

        messages = []
        for r in rows:
            tool_res = json.loads(r["tool_results"]) if r["tool_results"] and isinstance(r["tool_results"], str) else r["tool_results"]
            action_card = None
            if tool_res and isinstance(tool_res, list):
                for tr in tool_res:
                    if tr.get("result", {}).get("action_required") == "confirmation":
                        action_card = tr["result"]
            messages.append({
                "id": str(r["id"]),
                "sender": r["sender"],
                "content": r["content"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "action_card": action_card
            })

        return {"conversation_id": conversation_id, "messages": messages}

@router.post("/index-policies")
async def trigger_policy_indexing(user=Depends(get_current_user)):
    """Admin endpoint to index all policies for RAG semantic search."""
    if user.get("role") not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Only HR or Admin can index company policies.")
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        count = await index_all_policies(conn)
        return {"status": "success", "indexed_chunks": count}
