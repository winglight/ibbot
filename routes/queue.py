from fastapi import APIRouter, HTTPException

from core import queue_manager

router = APIRouter()

@router.get("/api/queue/status")
async def get_queue_status():
    """获取队列整体状态"""
    if not queue_manager:
        raise HTTPException(status_code=500, detail="Queue manager not initialized")

    status = queue_manager.get_queue_status()
    return status

@router.get("/api/queue/task/{task_id}")
async def get_task_status(task_id: str):
    """获取特定任务状态"""
    if not queue_manager:
        raise HTTPException(status_code=500, detail="Queue manager not initialized")
    
    task_status = queue_manager.get_task_status(task_id)
    if not task_status:
        raise HTTPException(status_code=404, detail="Task not found")
    
    return task_status

@router.get("/api/queue/tasks")
async def get_all_tasks():
    """获取所有任务列表"""
    if not queue_manager:
        raise HTTPException(status_code=500, detail="Queue manager not initialized")
    
    tasks = queue_manager.get_all_tasks()
    return {"tasks": tasks}
