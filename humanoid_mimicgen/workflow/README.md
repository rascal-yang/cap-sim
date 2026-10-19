# 独立的可复现操作流程

本目录是独立入口，不改 `cap_sim`、其他数据生成路线或历史 MimicGen 实验执行器。它组合策略存档、确定性几何检查、按需模型决策、冻结准备、真实 SONIC 执行和验收。

当前物理适配器明确支持 G1 / Dex3 / SONIC1.0 / 原生 MuJoCo 的单物体抓取、抬升、保持轨迹。操作手、物体、支撑面、阶段名称和帧范围均来自输入。其他机器人或操作能力不能因数据能解析就被视为支持。原动作可访问，结果属于**轨迹重定向**，不是从零生成操作策略。

## 接口

在 cap-sim 根目录使用已有 HUMANO wrapper：

```bash
./scripts/humano_python.sh -m humanoid_mimicgen.workflow plan --request request.json
./scripts/humano_python.sh -m humanoid_mimicgen.workflow prepare --request request.json --strategy-id <sha256>
./scripts/humano_python.sh -m humanoid_mimicgen.workflow execute --experiment <新实验目录>
./scripts/humano_python.sh -m humanoid_mimicgen.workflow inspect --experiment <实验目录>
```

CLI stdout 仅输出JSON，诊断写stderr；`execute` 返回0表示任务验收通过，1表示完成了失败试验，2表示输入/准备错误。Python调用同样的方法：

```python
from humanoid_mimicgen.workflow import service
proposal = service.plan(request)
prepared = service.prepare(proposal['strategy_id'], request)
result = service.execute(prepared['experiment_path'])
```

Python接口使用调用者提供的绝对路径或当前工作目录相对路径；CLI请求文件中的路径相对于该JSON文件。`inspect`不能仅凭目录存在断言进程仍活着。

## 请求数据

`files`包含 model、initial、targets、reference_states、task、alignment、record。record是有真实证据的物理实验记录，不能是未填写模板；继承的资产、物理、运行环境证据均再次校验。task中的原生环境与模型资源是部署依赖，其文件及模型版本必须保持匹配；策略包不会自动安装HUMANO或ONNX运行环境。

`strategy`含schema_version、parent_id、kind、adapter、context、bindings、phases、command_pose_offset、source_frame_count、policy、proposal、evidence、provenance。参见contracts.py的严格字段校验。

- `phases`采用命令半开区间 `[start,end)`，边界连续、无重复命令；`command_pose_offset`明确命令与测量位姿的对应。
- `bindings`按模型名称引用object/root_body/wrists/torso/feet/ground_geoms/support_geoms，hands指定实际操作手。
- `alignment`明确finger_source为executed_commands，不能将测量手指角度替代原命令。
- `policy`要求明确导航/停稳/规划/接近/物理门/任务验收/预算设置与origins，不提供针对苹果的隐式默认值。
- 确定性站位来自当前姿态和示范物体坐标系变换；其他候选来自保存的策略或模型提案。全部做IK/路径检查，不固定前移8cm、不固定偏移网格。

为了审阅已有证据，可从显式选择的历史记录构造请求：

```bash
./scripts/humano_python.sh -m humanoid_mimicgen.workflow.examples.from_record \
  --record <已验证历史实验>/experiment.json --out <新的请求目录> \
  --active-hand right --object <模型对象名> --support <支撑面geom名> \
  --speed 0.5 --stop-radius 0.10 --pose-offset -1
```

该工具只迁移已知grasp/lift/hold记录格式；它是输入导入器，**不是通用参数默认表**。速度和导航停止范围由调用者明确提供，接近速度取示范实测峰值，物理和验收标准有历史来源，调用者须审阅适用性。生成JSON使用相对文件引用，不依赖/tmp。

## 模型决策与重放

`decision.mode`可选：

- `deterministic`：只做几何计算，不能求得可行候选时返回unreachable。
- `responses`：配置model（显式GPT-6）、base_url、key_env、timeout_s。默认只在确定性候选失败时调用，request_alternatives为true时也可请求替代候选。绝不切换模型或读取其他凭据。
- `saved`：提供path和identity，提供同目录provenance.json，从保存的JSON提案读取，核对证据摘要，不调用网络。

站位模型输出接口提供 `propose_stances / needs_observation / abort`，引用scene/failed_geometry/source等证据；模型不能改变物理限制、增益、标注或执行任意代码。示范标注仍由原管线的annotation接口产生；当前决策接口不自动创造新的抓法或新的未标注接触轨迹。

另有 `resolve --request catalog.json`（Python：`selection.resolve(catalog)`）做技能选择和失败恢复提案。catalog包含：

```json
{
  "experiment_root": "experiments",
  "recovery_from": null,
  "decision": {"mode": "responses", "model": "gpt-6-astra", "base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY", "timeout_s": 60},
  "options": [{
    "id": "right_side_demonstration",
    "approach_description": "示范所定义的右手侧向接近",
    "initial_state_origin": "declared_initial",
    "request": {"...": "完整 plan 请求对象，包含已标注示范、操作手、源片段及其证据"}
  }]
}
```

上例是字段说明，`request`须替换成完整请求。API只能选择给定能力ID；不同手、接近方向和源片段由不同完整候选表示，不能凭空改写命令。选择后自动进行几何规划，返回 `selected_request` 和策略ID；使用该请求继续prepare。resolve不启动物理执行。`needs_observation/abort`保持为明确结果。

恢复时提供recovery_from失败实验目录；各候选parent_id必须指向失败策略，API输出failure_hypothesis和expected_condition。初态来源可为declared_initial（明确重新起始）或previous_terminal（完整检查点匹配）；无隐式重置。previous_terminal需包含qpos/qvel/ctrl/qacc_warmstart全部字段，缺少检查点则拒绝。保存的选择响应以同目录provenance.json绑定证据，离线重放不会再次请求模型。

Requests/Responses采用[官方结构化输出格式](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)，完整请求、原响应、解析结果、模型身份、耗时及错误均落盘。凭据只存在于环境变量和HTTP头。单元测试中的provider桩不等同于真实远端调用成功。

同输入复现直接调用 `prepare(saved_strategy_id, request)`，不会再次调用模型。场景/阶段/控制设置变更要求新策略；代码变化也要求恢复原版本或生成新修订。重新请求相同模型不保证相同决策，保存输出才是决策复现依据。

## 策略保存

策略保存在调用者指定的strategy_root/<sha256>/。manifest绑定策略内容与复制的输入、代码及规划证据。构建在临时目录完成后原子发布；校验失败不覆盖已有包。路径穿越、非有限数值、依赖篡改和不连续阶段被拒绝。

策略定义不写回执行结果；结果记录在strategy_root/executions/<strategy_id>/<experiment_id>.json中。parent_id用于表达新修订来源；失败的实验与反思保留。物理实验每次创建新的北京时间分钟ID，并先冻结合同；现有execution目录阻止重复执行。

## 补偿与标定

旧实验的积分增益0.5/s和2cm上限没有成为新流程默认值。`compensation.mode=none`明确关闭额外补偿，SONIC本身仍执行闭环控制。

可选 `identified_equilibrium` 从逐周期实测指令和响应辨识局部一阶模型，再求稳态逆映射。每只操作手须提供calibration_<hand> NPZ（command_xyz、measured_xyz、scope_json、dt_s），声明max_prediction_error_m。dt_s必须匹配控制周期。模型必须有足够激励、稳定响应并通过预测误差验收。scope绑定适配器、模型、初态和对象/手绑定，超过观测指令范围返回needs_calibration，不做外推或使用旧增益。该方法是新补偿方法，不能继承旧积分补偿的物理成功结论。

## 失败与验收

接近结束需满足policy.entry声明的实测末端位置、姿态及连续时长条件；等待受approach_s预算约束。抬升/保持的接触、离桌前置条件与连续保持验收独立记录，源时间本身不证明完成。

冻结前检查规划与实际执行的完整编译模型一致、实际三个CPU会话、模型/代码/输入哈希、编译物理参数和命名实体；真实执行仅SONIC mode1，无原token/mode0前缀，无重置或隐式回退。IK仅生成参考。

结果区分进程退出、轨迹执行完成、抓起保持、原生checker、视频质量。碰撞、失稳、抓前物体移动、接触丢失均停止。异常时保存已尝试与已完成命令，非有限测量写为null并拒绝成功；发布失败结果和反思。程序正常退出不代表抓取成功；未执行指标为null；训练视觉未验收时training_ready=false。

首个新入口实际运行：20261019_1946_workflow_native，真实执行1789物理步、481帧接触源命令，抬升阶段contact_lost，无碰桌，未成功抓起。它验证了执行与失败记录链路，**没有证明去掉旧经验参数后仍能成功**。旧成功配置和本次策略不同，不能作严格单一变量对照。

修订后的执行：`20261019_2004_workflow_native`，真实执行10000步（50秒），全程mode1。右手最终位置误差1.74cm、姿态误差0.0831rad；本次明确输入的姿态进入限值为0.03rad，未进入抓取而预算耗尽。这说明该导入夹具的进入条件与控制器响应不匹配，不能据此断言该位置不可抓，也不能把solver容差当成普适抓取容差。后续应以接触可行域或实测标定来定义任务允许误差，不能为通过测试静默调宽。

当前26个新测试、135个历史MimicGen测试和71个仓库测试通过；模型API仅以传输桩测试，未声称真实远端调用成功。

测试：

```bash
./scripts/humano_python.sh -m pytest humanoid_mimicgen/workflow/tests -q
./scripts/humano_python.sh -m pytest humanoid_mimicgen/tests -q
./scripts/humano_python.sh -m pytest tests -q
```

两个旧测试目录存在同名模块，分别执行避免导入冲突。正式十条件方法比较仍未由一次接口回归替代。
