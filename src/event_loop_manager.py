"""
事件循环管理器 - 处理Python 3.13和ib-async的兼容性问题

该模块提供了强健的事件循环管理机制，专门解决：
1. Python 3.13.7 + ib-async 2.0.1的兼容性问题
2. "Event loop is closed" 错误
3. "This event loop is already running" 错误
4. 多进程环境下的事件循环冲突
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import weakref
from typing import Any, Awaitable, Callable, Optional, TypeVar

__all__ = ["EventLoopManager", "setup_event_loop_policy", "get_safe_event_loop"]

logger = logging.getLogger(__name__)

T = TypeVar("T")


class EventLoopManager:
    """强健的事件循环管理器，专门处理ib-async兼容性问题"""
    
    _instance: Optional[EventLoopManager] = None
    _lock = threading.Lock()
    
    def __init__(self) -> None:
        self._loops: weakref.WeakSet[asyncio.AbstractEventLoop] = weakref.WeakSet()
        self._thread_loops: dict[int, asyncio.AbstractEventLoop] = {}
        self._setup_complete = False
    
    @classmethod
    def get_instance(cls) -> EventLoopManager:
        """获取单例实例"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance
    
    def setup_for_ib_async(self) -> None:
        """为ib-async设置兼容的事件循环策略"""
        if self._setup_complete:
            return
            
        try:
            # 在Windows上使用SelectorEventLoop以避免ProactorEventLoop的问题
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
                logger.info("设置Windows SelectorEventLoop策略以兼容ib-async")
            
            # 确保有一个主事件循环
            try:
                loop = asyncio.get_running_loop()
                logger.debug("使用现有的运行中事件循环")
            except RuntimeError:
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_closed():
                        raise RuntimeError("Event loop is closed")
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    logger.info("创建新的事件循环")
            
            self._register_loop(loop)
            self._setup_complete = True
            
        except Exception as e:
            logger.error(f"设置事件循环策略失败: {e}")
            raise
    
    def _register_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """注册事件循环以便跟踪"""
        self._loops.add(loop)
        thread_id = threading.get_ident()
        self._thread_loops[thread_id] = loop
    
    def get_or_create_loop(self) -> asyncio.AbstractEventLoop:
        """获取或创建适合当前线程的事件循环"""
        thread_id = threading.get_ident()
        
        # 首先尝试获取当前运行的循环
        try:
            loop = asyncio.get_running_loop()
            if not loop.is_closed():
                self._register_loop(loop)
                return loop
        except RuntimeError:
            pass
        
        # 检查是否有为当前线程缓存的循环
        if thread_id in self._thread_loops:
            loop = self._thread_loops[thread_id]
            if not loop.is_closed():
                return loop
            else:
                del self._thread_loops[thread_id]
        
        # 尝试获取默认循环
        try:
            loop = asyncio.get_event_loop()
            if not loop.is_closed():
                self._register_loop(loop)
                return loop
        except RuntimeError:
            pass
        
        # 创建新循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._register_loop(loop)
        logger.debug(f"为线程 {thread_id} 创建新的事件循环")
        return loop
    
    def run_coroutine_safely(self, coro: Awaitable[T], timeout: Optional[float] = None) -> T:
        """安全地运行协程，处理各种事件循环状态"""
        try:
            # 尝试在当前运行的循环中调度
            loop = asyncio.get_running_loop()
            if loop.is_running():
                # 如果循环正在运行，使用run_coroutine_threadsafe
                import concurrent.futures
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                return future.result(timeout=timeout)
        except RuntimeError:
            pass
        
        # 获取或创建循环并运行
        loop = self.get_or_create_loop()
        
        if loop.is_running():
            # 循环正在运行，在新线程中执行
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, coro)
                return future.result(timeout=timeout)
        else:
            # 循环未运行，直接运行
            return loop.run_until_complete(coro)
    
    def cleanup_closed_loops(self) -> None:
        """清理已关闭的事件循环"""
        closed_threads = []
        for thread_id, loop in self._thread_loops.items():
            if loop.is_closed():
                closed_threads.append(thread_id)
        
        for thread_id in closed_threads:
            del self._thread_loops[thread_id]
            logger.debug(f"清理线程 {thread_id} 的已关闭事件循环")


def setup_event_loop_policy() -> None:
    """设置兼容ib-async的事件循环策略"""
    manager = EventLoopManager.get_instance()
    manager.setup_for_ib_async()


def get_safe_event_loop() -> asyncio.AbstractEventLoop:
    """获取安全的事件循环"""
    manager = EventLoopManager.get_instance()
    return manager.get_or_create_loop()


def run_coroutine_safely(coro: Awaitable[T], timeout: Optional[float] = None) -> T:
    """安全地运行协程"""
    manager = EventLoopManager.get_instance()
    return manager.run_coroutine_safely(coro, timeout)


# 在模块导入时自动设置
try:
    setup_event_loop_policy()
except Exception as e:
    logger.warning(f"自动设置事件循环策略失败: {e}")