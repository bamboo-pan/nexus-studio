from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_static_frontend_uses_model_capabilities_for_controls():
    app_js = (ROOT / "src" / "aistudio_api" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")

    assert "selectedCaps" in app_js
    assert "applyModelCapabilities" in app_js
    assert "detail?.message" in app_js
    assert "d?.error?.message" in app_js
    assert "this.controlAvailable('search')&&this.cfg.search==='on'" in app_js
    assert "if(this.controlAvailable('thinking')) body.thinking=this.cfg.thinking" in app_js
    assert "this.cfg.thinking!=='off') body.thinking" not in app_js
    assert "ensureTextModelDefaults" in app_js
    assert "get preferredTextModelIds(){return['gemini-3-flash-preview','gemini-3.5-flash','gemma-4-31b-it']}" in app_js
    assert "preferredTextModel(textModels=this.textModels)" in app_js
    assert "this.model=preferred?.id||textModels[0].id" in app_js
    assert "selectModel(m.id)" in index_html
    assert "x-for=\"m in textModels\"" in index_html
    assert "暂无文本模型" in index_html
    assert "controlAvailable('thinking')" in index_html
    assert "controlAvailable('stream')" in index_html


def test_static_frontend_exposes_configurable_api_interfaces():
    app_js = (ROOT / "src" / "aistudio_api" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT / "src" / "aistudio_api" / "static" / "style.css").read_text(encoding="utf-8")

    assert "interfaceMode:'openai'" in app_js
    assert "aistudio.interfaceMode.v1" in app_js
    assert "aistudio.apiSelection.v1" not in app_js
    assert "validInterfaceMode(value)" in app_js
    assert "if(this.models.length)this.ensureTextModelDefaults();else this.applyModelCapabilities()" in app_js
    assert "id:'openai',label:'OpenAI 兼容'" in app_js
    assert "id:'responses',label:'OpenAI Responses'" in app_js
    assert "id:'gemini',label:'Gemini'" in app_js
    assert "id:'claude',label:'Claude'" in app_js
    assert "modelListEndpoint(mode=this.interfaceMode){return mode==='gemini'?'/v1beta/models':'/v1/models'}" in app_js
    assert "modelsLoading:false" in app_js
    assert "modelLoadSeq:0" in app_js
    assert "modelListUrl(refresh=false,mode=this.interfaceMode)" in app_js
    assert "const seq=++this.modelLoadSeq" in app_js
    assert "seq!==this.modelLoadSeq||mode!==this.interfaceMode" in app_js
    assert "refresh?'?refresh=true':''" in app_js
    assert "refreshModels(){return this.loadModels(true)}" in app_js
    assert "modelsLoading?'加载模型中'" in index_html
    assert "@click=\"refreshModels()\"" in index_html
    assert "刷新模型列表" in index_html
    assert ".model-picker-row" in style_css
    assert ".model-refresh-btn" in style_css
    assert "@keyframes spin" in style_css
    assert "normalizeGeminiModel(item)" in app_js
    assert "selectInterfaceMode(value)" in app_js
    assert "geminiChatRequestBody()" in app_js
    assert "openAiContentToGeminiParts(content)" in app_js
    assert "geminiChatEndpoint(stream=false)" in app_js
    assert "stream?'streamGenerateContent':'generateContent'" in app_js
    assert "completeGeminiChatFromCurrentMessages()" in app_js
    assert "completeOpenAIChatFromCurrentMessages()" in app_js
    assert "responsesRequestBody()" in app_js
    assert "responseOutputThinking(payload)" in app_js
    assert "response.reasoning.delta" in app_js
    assert "thinking:this.responseOutputThinking(d)||''" in app_js
    assert "completeResponsesChatFromCurrentMessages()" in app_js
    assert "claudeRequestBody()" in app_js
    assert "completeClaudeChatFromCurrentMessages()" in app_js
    assert "if(this.interfaceMode==='responses')" in app_js
    assert "if(this.interfaceMode==='claude')" in app_js
    assert "imageGenerationEndpoint(){return'/v1/images/generations'}" in app_js
    assert "this.fetchJson(this.imageGenerationEndpoint()" in app_js
    assert "接口模式" in index_html
    assert "interfaceModeOptions" in index_html
    assert "interfaceModeLabel||'OpenAI 兼容'" in index_html
    assert "selectInterfaceMode(option.id)" in index_html
    assert "模型接口" not in index_html
    assert "聊天接口" not in index_html
    assert "图片接口" not in index_html
    assert "modelApiOptions" not in index_html
    assert "chatApiOptions" not in index_html
    assert "imageApiOptions" not in index_html
    assert "selectModelApi(option.id)" not in index_html
    assert "selectChatApi(option.id)" not in index_html
    assert "selectImageApi(option.id)" not in index_html
    assert "selectModel(m.id)" in index_html


def test_static_frontend_request_logs_show_chain_phases_and_responses():
    app_js = (ROOT / "src" / "aistudio_api" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT / "src" / "aistudio_api" / "static" / "style.css").read_text(encoding="utf-8")

    assert "client_request:'用户 → 后端'" in app_js
    assert "upstream_response:'AI Studio → 后端'" in app_js
    assert "response_body_size" in app_js
    assert "requestLogEntries()" in app_js
    assert "requestPhaseMeta(item)" in app_js
    assert "activeRequestLog?.chain_id" in index_html
    assert "activeRequestLog?.status_code" in index_html
    assert "生命周期阶段" in index_html
    assert "响应 Body JSON" in index_html
    assert "response_body_raw" in index_html
    assert "selectImageModel(m.id)" in index_html
    assert "selectPromptOptimizerModel(m.id)" in index_html
    assert "openSelect==='imageModel'" in index_html
    assert "aria-disabled=\"true\" x-show=\"!imageModels.length\"" in index_html
    assert "api-toolbar" in style_css
    assert ".api-control" in style_css


def test_static_frontend_exposes_request_log_page():
    app_js = (ROOT / "src" / "aistudio_api" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT / "src" / "aistudio_api" / "static" / "style.css").read_text(encoding="utf-8")

    assert "requestLogEnabled:false" in app_js
    assert "requestLogEntryTotal:0" in app_js
    assert "requestLogSelection:{}" in app_js
    assert "d.group_count" in app_js
    assert "d.group_count??this.requestLogTotal||0" not in app_js
    assert "loadRequestLogStatus()" in app_js
    assert "toggleRequestLogging()" in app_js
    assert "loadRequestLogs()" in app_js
    assert "loadRequestLogDetail(id)" in app_js
    assert "selectedRequestLogCount" in app_js
    assert "selectAllRequestLogs()" in app_js
    assert "deleteSelectedRequestLogs()" in app_js
    assert "exportSelectedRequestLogs()" in app_js
    assert "requestFullJson()" in app_js
    assert "'/request-logs/status'" in app_js
    assert "`/request-logs/groups/${encodeURIComponent(id)}`" in app_js
    assert "'/request-logs/groups/delete'" in app_js
    assert "'/request-logs/export'" in app_js
    assert "view==='requests'" in index_html
    assert "请求记录" in index_html
    assert "完整请求" in index_html
    assert "保存开启" in index_html
    assert "导出所选" in index_html
    assert "删除所选" in index_html
    assert "Body JSON" in index_html
    assert "Body 原文" in index_html
    assert "完整记录" in index_html
    assert "复制完整 JSON" in index_html
    assert "导出当前" in index_html
    assert "删除当前" in index_html
    assert ".request-workspace" in style_css
    assert ".request-switch.active" in style_css
    assert ".request-log-check" in style_css
    assert ".request-phase-card" in style_css
    assert ".request-code" in style_css


def test_static_frontend_exposes_system_config_page():
    app_js = (ROOT / "src" / "aistudio_api" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT / "src" / "aistudio_api" / "static" / "style.css").read_text(encoding="utf-8")

    assert "view==='config'" in index_html
    assert "go('config')" in index_html
    assert "系统配置" in index_html
    assert "x-text=\"item.key\"" in index_html
    assert "x-show=\"!!item.configured_error\"" in index_html
    assert "loadConfig()" in app_js
    assert "saveConfigItem(item)" in app_js
    assert "resetConfigItem(item)" in app_js
    assert "'/config'" in app_js
    assert "`/config/${encodeURIComponent(item.key)}`" in app_js
    assert "configPendingRestartCount" in app_js
    assert ".config-row.pending" in style_css
    assert ".config-toggle" in style_css
    assert ".config-metrics" in style_css


def test_static_frontend_exposes_provider_manager_page_without_token_state():
    app_js = (ROOT / "src" / "aistudio_api" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT / "src" / "aistudio_api" / "static" / "style.css").read_text(encoding="utf-8")

    assert "view==='providers'" in index_html
    assert "go('providers')" in index_html
    assert "Provider Manager" in index_html
    assert "共享 Provider 池" in index_html
    assert "providerManagerProviders:[]" in app_js
    assert "providerManagerModels:[]" in app_js
    assert "providerManagerAudit:[]" in app_js
    assert "providerManagerDialogOpen:false" in app_js
    assert "providerManagerTokenVisible:false" in app_js
    assert "providerManagerDiscovering:false" in app_js
    assert "providerManagerApiBase(){return'/api/provider-manager'}" in app_js
    assert "loadProviderManager()" in app_js
    assert "newProviderManagerProvider()" in app_js
    assert "editProviderManagerProvider(provider=this.providerManagerSelectedProvider())" in app_js
    assert "toggleProviderManagerTokenVisible()" in app_js
    assert "discoverProviderManagerModels()" in app_js
    assert "providerManagerCatalogPayload()" in app_js
    assert "saveProviderManagerProvider()" in app_js
    assert "toggleProviderManagerProvider(provider)" in app_js
    assert "deleteProviderManagerProvider(provider)" in app_js
    assert "providerManagerCredentialLabel(provider)" in app_js
    assert "providerManagerAuditSummary(event)" in app_js
    assert "`${base}/providers`" in app_js
    assert "`${base}/model-catalog`" in app_js
    assert "`${this.providerManagerApiBase()}/model-catalog/discover`" in app_js
    assert "`${base}/audit`" in app_js
    assert "新建 provider" in index_html
    assert "role=\"dialog\"" in index_html
    assert "x-ref=\"providerManagerToken\"" in index_html
    assert "providerManagerTokenVisible?'text':'password'" in index_html
    assert "toggleProviderManagerTokenVisible()" in index_html
    assert "discoverProviderManagerModels()" in index_html
    assert "Aliases" in index_html
    assert "默认文本" in index_html
    assert "x-model=\"providerManagerDraft.token\"" not in index_html
    assert "token:''" not in app_js
    assert "finally{this.clearProviderManagerTokenField();this.providerManagerSaving=false}" in app_js
    assert "providerManagerDraft:{id:'',name:'',enabled:true,base_url:'',timeout:120,model_catalog:[]" in app_js
    assert "Model Catalog" in index_html
    assert "Audit" in index_html
    assert ".provider-workspace" in style_css
    assert ".provider-modal-backdrop" in style_css
    assert ".provider-draft-model" in style_css
    assert ".provider-row.active" in style_css
    assert ".provider-model-card" in style_css
    assert ".provider-audit-card" in style_css


def test_static_frontend_exposes_playground_workbench_tools():
    app_js = (ROOT / "src" / "aistudio_api" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT / "src" / "aistudio_api" / "static" / "style.css").read_text(encoding="utf-8")

    assert "applyChatPreset(name)" in app_js
    assert "clearChat()" in app_js
    assert "copyMessage(m)" in app_js
    assert "copyMessageText(m)" in app_js
    assert "copyMessageMarkdown(m)" in app_js
    assert "beginEditMessage(index)" in app_js
    assert "saveEditedMessage(index)" in app_js
    assert "rerunMessage(index)" in app_js
    assert "branchFromMessage(index)" in app_js
    assert "makeMessageApp(index)" in app_js
    assert "deleteMessage(index)" in app_js
    assert "completeChatFromCurrentMessages()" in app_js
    assert "chatMessageApiContent(text,files=[])" in app_js
    assert "chatRunModeSummary" in app_js
    assert "chatSessions:[]" in app_js
    assert "loadChatSessions()" in app_js
    assert "aistudio.chatSessions.v1" in app_js
    assert "restoreChatSession(session)" in app_js
    assert "deleteChatSession(session)" in app_js
    assert "newChatSession()" in app_js
    assert "chatUsageRows" in app_js
    assert "messageUsageRows(message)" in app_js
    assert "cached_tokens" in app_js
    assert "if(d.usage&&(!d.choices||!d.choices.length))" in app_js
    assert "if(d.error){this.msgs[idx].error=d.error.message||JSON.stringify(d.error);continue}" in app_js
    assert "chatRequestSummary" not in app_js
    assert "chatCapabilityItems" in app_js
    assert "模型调试工作台" in index_html
    assert "本地会话" in index_html
    assert "Token 统计" in index_html
    assert "messageUsageRows(m)" in index_html
    assert "@click=\"newChatSession()\"" in index_html
    assert "@click=\"restoreChatSession(session)\"" in index_html
    assert "@click=\"deleteChatSession(session)\"" in index_html
    assert "@click=\"togglePlaygroundSideCollapsed()\"" in index_html
    assert "playgroundSideCollapsed" in index_html
    assert "side-collapsed" in index_html
    assert "@click=\"toggleMessageMenu(i,$event)\"" in index_html
    assert "@click=\"beginEditMessage(i)\"" in index_html
    assert "@click=\"rerunMessage(i)\"" in index_html
    assert "@click=\"branchFromMessage(i)\"" in index_html
    assert "@click=\"makeMessageApp(i)\"" in index_html
    assert "@click=\"copyMessageText(m)\"" in index_html
    assert "@click=\"copyMessageMarkdown(m)\"" in index_html
    assert "@click=\"deleteMessage(i)\"" in index_html
    assert "Make this an app" in index_html
    assert "x-text=\"chatRunModeSummary\"" in index_html
    assert "请求摘要" not in index_html
    assert "playground-metrics" not in index_html
    assert "Chat Settings" not in index_html
    assert "cfg-dropdown" not in index_html
    assert "configOpen" not in app_js
    assert "@click=\"applyChatPreset('balanced')\"" in index_html
    assert "playground-shell" in style_css
    assert "grid-template-columns:minmax(0,1fr) 340px" in style_css
    assert ".playground-shell.side-collapsed{grid-template-columns:minmax(0,1fr) 74px}" in style_css
    assert ".playground-shell,.playground-shell.side-collapsed{grid-template-columns:1fr" in style_css
    assert ".playground-side.is-collapsed{width:74px" in style_css
    assert ".playground-side-toggle" in style_css
    assert ".msg-menu" in style_css
    assert ".msg-edit" in style_css
    assert ".chat-session-list" in style_css


def test_static_frontend_exposes_local_studio_workbench():
    app_js = (ROOT / "src" / "aistudio_api" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT / "src" / "aistudio_api" / "static" / "style.css").read_text(encoding="utf-8")

    assert "<title>Nexus Studio</title>" in index_html
    assert '<span class="sidebar-title">Nexus Studio</span>' in index_html
    assert "/static/app.js?v=20260601-provider-manager" in index_html
    assert "OpenAI Local Studio" not in index_html
    assert "view==='studio'" in index_html
    assert "go('studio')" in index_html
    assert "openai.localStudio.settings.v1" in app_js
    assert "providerProfiles" not in app_js
    assert "activeProviderId" not in app_js
    assert "active_provider_id" not in app_js
    assert "provider_id:this.localStudioProviderId,providerType" not in app_js
    assert "provider_type:active.type" not in app_js
    assert "image_model:this.localStudioImageModel" not in app_js
    assert "localStudioProviders:[]" in app_js
    assert "localStudioSettings:{name:'',baseUrl:'',apiKey:'',timeout:300}" in app_js
    assert "localStudioProviderType:'google-ai-studio'" in app_js
    assert "timeout:300,interfaceMode:'responses'" in app_js
    assert "providerType==='google-ai-studio'?Math.max(rawTimeout,300):rawTimeout" in app_js
    assert "provider_type:providerType" in app_js
    assert "openai-provider-${index+1}" in app_js
    assert "rawId&&!rawId.startsWith('google-ai-studio')" in app_js
    assert "provider_id:this.localStudioProviderId" in app_js
    assert "selectLocalStudioProvider(provider.id)" in index_html
    assert "addLocalStudioProvider()" in index_html
    assert "removeLocalStudioProvider()" in index_html
    assert "Provider Type" in index_html
    assert "loadLocalStudioModels()" in app_js
    assert "'/api/local-studio/models'" in app_js
    assert "'/api/local-studio/conversations'" in app_js
    assert "'/api/local-studio/conversations/bulk-delete'" in app_js
    assert "'/api/local-studio/chat'" in app_js
    assert "localStudioModelOptions" in app_js
    assert "localStudioInterfaceMode:'responses'" in app_js
    assert "interfaceMode:this.localStudioInterfaceMode" in app_js
    assert "selectLocalStudioInterfaceMode(value)" in app_js
    assert "localStudioStream:'on'" in app_js
    assert "localStudioControlAvailable('stream')" in app_js
    assert "localStudioSearch:'off'" in app_js
    assert "search:this.localStudioSearch==='on'" in app_js
    assert "localStudioCacheEnabled" not in app_js
    assert "localStudioCacheNamespace" not in app_js
    assert "cacheEnabled:true" not in app_js
    assert "cache_enabled:true" not in app_js
    assert "cache_namespace:this.localStudioCacheNamespace" not in app_js
    assert "Cache Namespace" not in index_html
    assert "Web search" in index_html
    assert "Local request cache" not in index_html
    assert "localStudioCacheEnabled=!localStudioCacheEnabled" not in index_html
    assert "sendLocalStudioStream(body)" in app_js
    assert "local_studio.delta" in app_js
    assert "const streamMessage={id:`stream-${Date.now()}`" in app_js
    assert "const message=this.localStudioActiveMessages[this.localStudioActiveMessages.length-1]||streamMessage" in app_js
    assert "this.refreshLocalStudioStreamMessage();this.scrollLocalStudioDown()" in app_js
    assert "localStudioPendingTitle" in app_js
    assert "localStudioBusyTimer" in app_js
    assert "Local Studio 流式响应中断，已保存结果" in app_js
    assert "localStudioMessageContent(message)" in app_js
    assert "localStudioMessageError(message)" in app_js
    assert "this.localStudioDraft=''" in app_js
    assert "appendLocalStudioMessage" in app_js
    assert "localStudioOptimisticFiles(files)" in app_js
    assert "id:`local-user-${Date.now()}" in app_js
    assert "this.localStudioInterfaceMode=this.validInterfaceMode(this.localStudioConversation.interface_mode)" in app_js
    assert "startsWith('gpt-image-')" in app_js
    assert "gpt-image-2" in app_js
    assert "localStudioInterfaceModeLabel" in index_html
    assert "selectLocalStudioInterfaceMode(option.id)" in index_html
    assert "localStudioStream==='on'" in index_html
    assert "localStudioSelectedCaps" in index_html
    assert "local-studio-waiting" in index_html
    assert "localStudioStatusText" in index_html
    assert "localStudioMessageError(message)" in index_html
    assert "localStudioInterfaceMode==='responses'" in index_html
    assert "localStudioImageModels:[]" in app_js
    assert "localStudioImageModel:''" in app_js
    assert "localStudioImageModelOptions" in app_js
    assert "defaultLocalStudioImageModels()" in app_js
    assert "gemini-3.1-flash-image-preview" in app_js
    assert "gemini-3-pro-image-preview" in app_js
    assert "localStudioImageToolTitle" in app_js
    assert "localStudioImageToolLabel" in app_js
    assert "Image Model" in index_html
    assert "x-for=\"model in localStudioImageModelOptions\"" in index_html
    assert "localStudioSizeOptions" in app_js
    assert "size:'2560x1440'" in app_js
    assert "size:'3824x2144'" in app_js
    assert "3840x2160" not in app_js
    assert "localStudioImageCustomSize" in app_js
    assert "localStudioResolvedImageSize()" in app_js
    assert "x-for=\"option in localStudioSizeOptions\"" in index_html
    assert "Custom Size" in index_html
    assert "below 3840px" in app_js
    assert "reasoning_effort:this.localStudioControlAvailable('thinking')?this.localStudioReasoningEffort:'off'" in app_js
    assert "image_tool_enabled:this.localStudioInterfaceMode==='responses'&&this.localStudioImageToolEnabled" in app_js
    assert "image_tool_provider:this.localStudioProviderType" in app_js
    assert "image_model:this.localStudioSelectedImageModel.id||this.localStudioImageModel" in app_js
    assert "if(!this.localStudioIsGoogleProvider)" in app_js
    assert "localStudioImageParamAvailable('quality')" in index_html
    assert "rerunLocalStudioMessage(index)" in app_js
    assert "attachLocalStudioFiles" in app_js
    assert "image.b64_json||image.b64||image.result" in app_js
    assert "'/api/local-studio/assets/'" in app_js
    assert "localStudioMessageImages(message)" in index_html
    assert "localStudioAttachments(message)" in index_html
    assert ".local-studio-shell" in style_css
    assert "grid-template-columns:300px minmax(0,1fr) 340px" in style_css
    assert ".local-studio-transcript" in style_css
    assert ".local-studio-waiting" in style_css
    assert ".local-studio-spinner" in style_css
    assert ".local-studio-grid-controls" in style_css
    assert ".chat-usage-grid" in style_css
    assert ".msg-usage" in style_css
    assert ".runtime-toggle>button.active" in style_css
    assert "chat" + "Templates" not in app_js
    assert "usePrompt" + "Template(template)" not in app_js
    assert "prompt-" + "template-card" not in index_html
    assert "@click=\"usePrompt" + "Template(template)\"" not in index_html
    assert ".prompt-" + "template-grid" not in style_css
    assert "playground-" + "empty-mark" not in index_html
    assert "选择" + "一个起点" not in index_html


def test_static_frontend_exposes_collapsible_sidebar():
    app_js = (ROOT / "src" / "aistudio_api" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT / "src" / "aistudio_api" / "static" / "style.css").read_text(encoding="utf-8")

    assert "sidebarCollapsed:false" in app_js
    assert "loadSidebarPreference()" in app_js
    assert "toggleSidebarCollapsed()" in app_js
    assert "aistudio.sidebarCollapsed" in app_js
    assert ":class=\"{'sidebar-collapsed':sidebarCollapsed}\"" in index_html
    assert "sidebar-toggle" in index_html
    assert "nav-label" in index_html
    assert "sidebar-footer-label" in index_html
    assert ".sidebar-collapsed .sidebar{width:72px" in style_css
    assert ".sidebar-collapsed .sidebar-title,.sidebar-collapsed .nav-label,.sidebar-collapsed .sidebar-footer-label{display:none}" in style_css
    assert ".sidebar-collapsed .sidebar-toggle svg{transform:rotate(180deg)}" in style_css
    assert ".sidebar-collapsed .sidebar{width:300px}" in style_css


def test_static_frontend_renders_playground_markdown_safely():
    app_js = (ROOT / "src" / "aistudio_api" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT / "src" / "aistudio_api" / "static" / "style.css").read_text(encoding="utf-8")

    assert "escapeHtml(value)" in app_js
    assert "safeMarkdownUrl(value)" in app_js
    assert "renderMarkdownInline(value)" in app_js
    assert "markdownToHtml(value)" in app_js
    assert "messageBodyHtml(m)" in app_js
    assert "imageTokens=[]" in app_js
    assert "<img class=\"markdown-image\"" in app_js
    assert "src=\"${this.escapeHtml(safe)}\"" in app_js
    assert "safe?`<img class=\"markdown-image\"" in app_js
    assert "javascript|vbscript|data" in app_js
    assert "target=\"_blank\"" in app_js
    assert "x-html=\"messageBodyHtml(m)\"" in index_html
    assert "'markdown-body':!m.error&&m.role!=='user'" in index_html
    assert "x-text=\"m.error||m.content\"" not in index_html
    assert ".msg-body.markdown-body" in style_css
    assert ".markdown-body pre" in style_css
    assert ".markdown-body img.markdown-image" in style_css
    assert "white-space:normal" in style_css


def test_static_frontend_exposes_account_health_tier_controls():
    app_js = (ROOT / "src" / "aistudio_api" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")

    assert "testAccount(a)" in index_html
    assert "updateTier(a,v)" in index_html
    assert "healthLabel(a.health_status)" in index_html
    assert "tierLabel(a.tier)" in index_html
    assert "testAccount(a)" in app_js
    assert "/test`" in app_js
    assert "凭据检查通过，生成权限以预热和真实请求为准" in app_js
    assert "账号检查通过" not in app_js
    assert "updateTier(a,tier)" in app_js


def test_static_frontend_exposes_exhaustion_mode_and_resolution_usage():
    app_js = (ROOT / "src" / "aistudio_api" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT / "src" / "aistudio_api" / "static" / "style.css").read_text(encoding="utf-8")

    assert "exhaustion" in app_js
    assert "耗尽模式" in app_js
    assert "['exhaustion','round_robin','lru','least_rl']" in index_html
    assert "rotationHint(rotCfg.mode)" in index_html
    assert "imageSizeEntries(a)" in index_html
    assert "accountImageSizeTotals" in app_js
    assert "statsTotals" in app_js
    assert "model-stats-panel" in index_html
    assert "totalReqs" in index_html
    assert "totalRL" in index_html
    assert "go('dashboard')" not in index_html
    assert "view==='dashboard'" not in index_html
    assert "if(route==='dashboard'){this.go('accounts');return}" in app_js
    assert "['chat','images','dashboard','accounts']" not in app_js
    assert "image_sizes" in app_js
    assert "refreshRuntimeStats()" in app_js
    assert "resolution-chip" in index_html
    assert ".rotation-option.active" in style_css
    assert ".resolution-chip" in style_css
    assert ".model-stats-panel" in style_css


def test_static_frontend_shows_pool_status_and_account_load():
    app_js = (ROOT / "src" / "aistudio_api" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT / "src" / "aistudio_api" / "static" / "style.css").read_text(encoding="utf-8")

    assert "totalAffinityLoad" in app_js
    assert "accountLoad(a)" in app_js
    assert "poolStatusLabel(a)" in app_js
    assert "poolStatusClass(a)" in app_js
    assert "poolStatusDetail(a)" in app_js
    assert "可调度" in app_js
    assert "默认账号" in app_js
    assert "绑定${Math.round(a.affinity_ttl_seconds/60)}分钟过期" in app_js
    assert "账号负载" in index_html
    assert "<th>负载</th>" in index_html
    assert "<th>池状态</th>" in index_html
    assert "绑定用户/会话" in index_html
    assert "default-account-note" in index_html
    assert "poolStatusLabel(a)" in index_html
    assert "poolStatusDetail(a)" in index_html
    assert "<th>状态</th>" not in index_html
    assert "待命" not in index_html
    assert ".load-stack" in style_css
    assert ".default-account-note" in style_css


def test_static_frontend_exposes_image_upload_and_generation_page():
    app_js = (ROOT / "src" / "aistudio_api" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT / "src" / "aistudio_api" / "static" / "style.css").read_text(encoding="utf-8")

    assert "attachChatFiles($event)" in index_html
    assert "$refs.chatFileInput.click()" in index_html
    assert ":accept=\"chatFileAccept\"" in index_html
    assert "chatCanSend" in app_js
    assert "file.isImage?{type:'image_url'" in app_js
    assert "{type:'file',file:{file_data:file.url,filename:file.name,mime_type:file.mime}}" in app_js
    assert "selectedCaps.file_input" in app_js
    assert "file_input_mime_types" in app_js
    assert "chatFileUploadEnabled" in app_js
    assert "applyRouteHash" in app_js
    assert "hashchange" in app_js
    assert "selectImageModel(m.id)" in index_html
    assert "x-model.number=\"imageCount\"" in index_html
    assert ":min=\"imageCountMin\"" in index_html
    assert ":max=\"imageCountMax\"" in index_html
    assert "imageCountHint" in index_html
    assert "imageTimeout:''" in app_js
    assert "imageTimeoutSummary" in app_js
    assert "normalizeImageTimeout()" in app_js
    assert "if(timeout)body.timeout=timeout" in app_js
    assert "this.imageTimeout=this.imageLastRequest.timeout?String(this.imageLastRequest.timeout):''" in app_js
    assert "this.imageLastRequest?.timeout?String(this.imageLastRequest.timeout):''" in app_js
    assert "超时秒数" in index_html
    assert "x-model=\"imageTimeout\"" in index_html
    assert "@change=\"normalizeImageTimeout()\"" in index_html
    assert "imageResponseFormat" in index_html
    assert "imageGenerationMeta" in app_js
    assert "response_format:this.imageResponseFormat" in app_js
    assert "x-text=\"imageSize\"" in index_html
    assert "/v1/images/generations" in app_js
    assert "retryLastImage()" in index_html
    assert "downloadImage(item)" in index_html
    assert "retryImage(item)" in index_html
    assert "localStorage.getItem('aistudio.imageHistory')" in app_js
    assert "clearImageHistory()" in index_html
    assert "lightweightImageItem" in app_js
    assert "selectedHistoryItems" in app_js
    assert "downloadSelectedImages()" in index_html
    assert "deleteSelectedImages()" in index_html
    assert "deleteHistoryImage(item)" in index_html
    assert "imageHistorySelection" in app_js


def test_static_frontend_exposes_image_prompt_templates_and_optimizer():
    app_js = (ROOT / "src" / "aistudio_api" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT / "src" / "aistudio_api" / "static" / "style.css").read_text(encoding="utf-8")

    assert "imageStyleTemplate:'none'" in app_js
    assert "imageStyleTemplates" in app_js
    assert "photorealistic" in app_js
    assert "comic" in app_js
    assert "textModels" in app_js
    assert "promptOptimizerSupportsThinking" in app_js
    assert "optimizeImagePrompt()" in app_js
    assert "finally{await this.refreshRuntimeStats();this.imagePromptOptimizing=false}" in app_js
    assert "finally{await this.refreshRuntimeStats();this.imageBusy=false}" in app_js
    assert "applyImagePromptOption(option)" in app_js
    assert "this.imagePromptOptions=[]" in app_js
    assert "/v1/images/prompt-optimizations" in app_js
    assert "style_template:this.imageStyleTemplate" in app_js
    assert "thinking:this.promptOptimizerSupportsThinking?this.imagePromptOptimizerThinking:'off'" in app_js
    assert "const images=await this.imageRequestImages();if(images.length)body.images=images" in app_js
    assert "imagePromptForRequest(prompt)" in app_js
    assert "Style template:" in app_js
    assert "promptOptionApplied" in app_js
    assert "风格模板" in index_html
    assert "提示词优化" in index_html
    assert "优化模型" in index_html
    assert "selectPromptOptimizerModel(m.id)" in index_html
    assert "@click=\"optimizeImagePrompt()\"" in index_html
    assert "@click=\"applyImagePromptOption(option)\"" in index_html
    assert "imagePromptOptions" in index_html
    assert "imagePromptOptimizerThinking" in index_html
    assert ".image-prompt-optimizer" in style_css
    assert ".prompt-option-card" in style_css
    assert ".optimizer-controls" in style_css
    assert "sameOriginRequestPath" in app_js
    assert "explicit&&item?.path" in app_js
    assert "attachImageReferences($event)" in index_html
    assert "imageEditReferences" in app_js
    assert "imageRequestImages" in app_js
    assert "body.images=images" in app_js
    assert "setBaseImage(item)" in index_html
    assert "pinImageReference(item,'history')" in index_html
    assert "pinImageReference(item,'result')" in index_html
    assert "pinSelectedHistory()" in index_html
    assert "clearImageEditSession()" in index_html
    assert "imageConversation" in app_js
    assert "imageSessions:[]" in app_js
    assert "activeImageSessionId" in app_js
    assert "loadImageSessions()" in app_js
    assert "loadImageSessions(false)" in app_js
    assert "saveCurrentImageSession(prompt)" in app_js
    assert "this.imagePrompt=''" in app_js
    assert "this.imageResults=[]" in app_js
    assert "fetchJson('/image-sessions')" in app_js
    assert "`/image-sessions/${encodeURIComponent(this.activeImageSessionId)}`" in app_js
    assert "restoreImageSession(session)" in app_js
    assert "deleteImageSession(session)" in app_js
    assert "imagePreview:null" in app_js
    assert "openImagePreview(item)" in app_js
    assert "closeImagePreview()" in app_js
    assert "@click=\"openImagePreview(item)\"" in index_html
    assert "@keydown.escape.window=\"closeImagePreview()\"" in index_html
    assert "image-preview-overlay" in index_html
    assert "会话历史" in index_html
    assert "已保存会话" in index_html
    assert index_html.index("上下文") < index_html.index("会话历史")
    assert "image-new-session-btn" in index_html
    assert "image-panel-actions" in style_css
    assert "imageSessions.length" in index_html
    assert "restoreImageSession(session)" in index_html
    assert "deleteImageSession(session)" in index_html
    assert "image-session-history" in style_css
    assert "image-session-card" in style_css
    assert ".image-thumb img{width:100%;height:100%;object-fit:contain" in style_css
    assert ".image-preview-img{max-width:100%;max-height:100%;object-fit:contain" in style_css
    assert "编辑会话" in index_html
    assert "b64_json" not in app_js.split("return{id:item.id||path||url,url,path,delete_url:item.delete_url||url", 1)[1].split("}", 1)[0]


def test_static_frontend_custom_select_supports_keyboard_and_scrollable_image_menu():
    app_js = (ROOT / "src" / "aistudio_api" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT / "src" / "aistudio_api" / "static" / "style.css").read_text(encoding="utf-8")

    assert "handleSelectKeydown" in app_js
    assert "ArrowDown" in app_js
    assert "Spacebar" in app_js
    assert "scrollIntoView({block:'nearest'})" in app_js
    assert "x-for=\"s in imageSizes\"" in index_html
    assert "aria-disabled=\"true\" x-show=\"!imageModels.length\"" in index_html
    assert "overscroll-behavior:contain" in style_css
    assert ".cselect-opt:hover,.cselect-opt.highlighted" in style_css
    assert "引导式 Studio" in index_html
    assert "image-studio-compose" in index_html
    assert "imageSubmitHint" in app_js
    assert "imageRunSummary" in app_js
    assert ".image-form-panel{grid-column:1;grid-row:1 / span 2;position:relative;overflow:visible" in style_css
    assert ".image-studio-controls{grid-template-columns:1fr 1fr" in style_css
    assert "position:sticky" not in style_css.split("/* Image generation */", 1)[1].split("/* Toast */", 1)[0]
    assert "@media(max-width:960px)" in style_css


def test_static_frontend_uses_dynamic_desktop_layouts():
    index_html = (ROOT / "src" / "aistudio_api" / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT / "src" / "aistudio_api" / "static" / "style.css").read_text(encoding="utf-8")

    assert ".account-pad{max-width:none" in style_css
    assert ".image-studio-pad{max-width:none" in style_css
    assert "image-main-column" in index_html
    assert "image-side-column" in index_html
    assert ".image-main-column,.image-side-column{min-width:0;display:flex;flex-direction:column;gap:18px}" in style_css
    assert "@media(min-width:1440px)" in style_css
    assert ".image-workspace{grid-template-columns:minmax(380px,460px) minmax(520px,1.45fr) minmax(300px,.75fr);grid-auto-flow:dense}" in style_css
    assert ".image-main-column{grid-column:2;grid-row:1}" in style_css
    assert ".image-side-column{grid-column:3;grid-row:1}" in style_css
    assert ".image-result-gallery{grid-template-columns:repeat(auto-fit,minmax(360px,1fr))}" in style_css
    assert "@media(min-width:1840px)" in style_css
    assert ".image-result-card .image-thumb{aspect-ratio:auto;min-height:260px}" in style_css
    assert ".image-result-card .image-thumb img{width:auto;height:auto;max-width:100%;max-height:min(70vh,760px);object-fit:contain}" in style_css