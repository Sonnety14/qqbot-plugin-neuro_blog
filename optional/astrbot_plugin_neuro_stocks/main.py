import asyncio
import contextlib
import json
from pathlib import Path
from aiohttp import web
from astrbot.api import logger
from astrbot.api.star import Star, StarTools, register
from .service import Service, control_app


@register('astrbot_plugin_neuro_stocks','Sonnety14','Neuro 历史美股游戏：周三集中回放与周记','0.1.0')
class NeuroStocks(Star):
    def __init__(self,context,config):
        super().__init__(context)
        self.context=context;self.config=config;self.task=None;self.runner=None;self.service=None

    async def initialize(self):
        root=StarTools.get_data_dir('astrbot_plugin_neuro_stocks')
        private=Path(self.config.get('private_config') or root/'private.json')
        cfg=json.loads(private.read_text(encoding='utf-8')) if private.exists() else {}
        cfg.setdefault('schedule_mode','weekly_batch')
        cfg.setdefault('public_dir',str(StarTools.get_data_dir('astrbot_plugin_neuro_blog')/'public'/'stocks'))
        def blog():
            star=self.context.get_registered_star('astrbot_plugin_neuro_blog')
            plugin=getattr(star,'star_cls',None)
            if not plugin:raise RuntimeError('Install the companion neuro_blog plugin first')
            return plugin
        async def persona():
            item=await self.context.persona_manager.get_persona(cfg.get('persona_id') or blog().config.get('persona_id',''))
            return item.system_prompt
        async def stickers():
            from importlib import import_module
            prefix=__package__.rsplit('.',1)[0]+'.' if '.' in __package__ else ''
            resources=import_module(prefix+'astrbot_plugin_neuro_blog.resources')
            picker=import_module(prefix+'astrbot_plugin_neuro_blog.sticker_picker')
            return picker.sticker_pools(resources.load_stickers(blog().config))
        self.service=Service(root,cfg,persona,stickers)
        self.service.publisher.index()
        if cfg.get('control_token'):
            self.runner=web.AppRunner(control_app(self.service,cfg['control_token']),access_log=None)
            await self.runner.setup()
            await web.TCPSite(self.runner,cfg.get('control_host','127.0.0.1'),cfg.get('control_port',18791)).start()
        else:logger.info('[neuro-stocks] awaiting private control configuration; game remains off')
        self.task=asyncio.create_task(self.loop())

    async def loop(self):
        while True:
            try:await self.service.tick()
            except Exception as exc:logger.warning('[neuro-stocks] paused operation: %s',type(exc).__name__)
            await asyncio.sleep(30)

    async def terminate(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):await self.task
        if self.runner:await self.runner.cleanup()
        if self.service:self.service.game.close()
