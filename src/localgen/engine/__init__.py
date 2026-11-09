from dataclasses import dataclass


@dataclass(frozen=True)
class EngineStatus:
    state: str = "stopped"
    code: str = "ENGINE_STOPPED"
    device: str | None = None


@dataclass
class Observation:
    image: bytes | None = None
    progress: tuple[int, int] | None = None
    # 进度阶段：sd-cli 在采样前要花几十秒加载权重/编码提示词，界面需要分得清
    # "还在加载模型" 和 "已经在采样"，否则那段时间看起来跟卡死一样。
    stage: str = "sampling"
    detail: str | None = None
