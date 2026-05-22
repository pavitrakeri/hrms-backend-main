from pydantic import BaseModel
from typing import Optional, List
from datetime import date, datetime


class ProjectCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    department_id: Optional[str] = None
    start_date: Optional[date] = None
    deadline: Optional[date] = None
    member_ids: Optional[List[str]] = None


class ProjectUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    start_date: Optional[date] = None
    deadline: Optional[date] = None
    member_ids: Optional[List[str]] = None


class ProjectMemberRow(BaseModel):
    user_id: str
    email: str
    full_name: Optional[str] = None
    role: str


class ProjectRow(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    created_by: str
    creator_email: Optional[str] = None
    creator_name: Optional[str] = None
    department_id: Optional[str] = None
    department_name: Optional[str] = None
    start_date: Optional[str] = None
    deadline: Optional[str] = None
    assigned_to: Optional[str] = None
    status: str
    task_count: int = 0
    open_task_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ProjectDetailResponse(BaseModel):
    project: ProjectRow
    members: List[ProjectMemberRow]


class ProjectListResponse(BaseModel):
    projects: List[ProjectRow]


class ProjectCreateResponse(BaseModel):
    status: str
    message: str
    project_id: str


class TaskCreateRequest(BaseModel):
    title: str
    description: Optional[str] = None
    assignee_id: Optional[str] = None
    due_date: Optional[date] = None
    status: Optional[str] = "todo"


class TaskUpdateRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    assignee_id: Optional[str] = None
    due_date: Optional[date] = None
    status: Optional[str] = None


class TaskStatusUpdateRequest(BaseModel):
    status: str


class TaskRow(BaseModel):
    id: str
    project_id: str
    project_name: Optional[str] = None
    title: str
    description: Optional[str] = None
    assignee_id: Optional[str] = None
    assignee_email: Optional[str] = None
    assignee_name: Optional[str] = None
    created_by: str
    creator_email: Optional[str] = None
    creator_name: Optional[str] = None
    due_date: Optional[str] = None
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class TaskListResponse(BaseModel):
    tasks: List[TaskRow]


class TaskDetailResponse(BaseModel):
    task: TaskRow


class TaskCreateResponse(BaseModel):
    status: str
    message: str
    task_id: str
