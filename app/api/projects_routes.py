from fastapi import APIRouter, Depends, BackgroundTasks, Query
from typing import Optional

from app.db import get_db_pool
from app.features.auth import get_current_user
from app.datamodels.project_models import (
    ProjectCreateRequest,
    ProjectUpdateRequest,
    ProjectCreateResponse,
    ProjectListResponse,
    ProjectDetailResponse,
    TaskCreateRequest,
    TaskUpdateRequest,
    TaskStatusUpdateRequest,
    TaskCreateResponse,
    TaskListResponse,
    TaskDetailResponse,
)
from app.features import projects as projects_service
from app.features import tasks as tasks_service

router = APIRouter(tags=["Projects & Tasks"])


@router.post("/projects", response_model=ProjectCreateResponse)
async def create_project(req: ProjectCreateRequest, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await projects_service.create_project(conn, user, req)


@router.get("/projects", response_model=ProjectListResponse)
async def list_projects(
    include_archived: bool = Query(False),
    user=Depends(get_current_user),
):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await projects_service.list_projects(conn, user, include_archived)


@router.get("/projects/{project_id}", response_model=ProjectDetailResponse)
async def get_project(project_id: str, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await projects_service.get_project_detail(conn, user, project_id)


@router.put("/projects/{project_id}", response_model=ProjectDetailResponse)
async def update_project(
    project_id: str,
    req: ProjectUpdateRequest,
    user=Depends(get_current_user),
):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await projects_service.update_project(conn, user, project_id, req)


@router.patch("/projects/{project_id}/archive")
async def archive_project(project_id: str, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await projects_service.archive_project(conn, user, project_id)


@router.post("/projects/{project_id}/tasks", response_model=TaskCreateResponse)
async def create_task(
    project_id: str,
    req: TaskCreateRequest,
    bg: BackgroundTasks,
    user=Depends(get_current_user),
):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await tasks_service.create_task(conn, user, project_id, req, bg)


@router.get("/projects/{project_id}/tasks", response_model=TaskListResponse)
async def list_project_tasks(
    project_id: str,
    status: Optional[str] = Query(None),
    user=Depends(get_current_user),
):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await tasks_service.list_project_tasks(conn, user, project_id, status)


@router.get("/tasks/my", response_model=TaskListResponse)
async def get_my_tasks(
    status: Optional[str] = Query(None),
    user=Depends(get_current_user),
):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await tasks_service.get_my_tasks(conn, user, status)


@router.get("/tasks/{task_id}", response_model=TaskDetailResponse)
async def get_task(task_id: str, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await tasks_service.get_task(conn, user, task_id)


@router.put("/tasks/{task_id}", response_model=TaskDetailResponse)
async def update_task(
    task_id: str,
    req: TaskUpdateRequest,
    bg: BackgroundTasks,
    user=Depends(get_current_user),
):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await tasks_service.update_task(conn, user, task_id, req, bg)


@router.patch("/tasks/{task_id}/status", response_model=TaskDetailResponse)
async def update_task_status(
    task_id: str,
    req: TaskStatusUpdateRequest,
    bg: BackgroundTasks,
    user=Depends(get_current_user),
):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await tasks_service.update_task_status(conn, user, task_id, req.status, bg)


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await tasks_service.delete_task(conn, user, task_id)
