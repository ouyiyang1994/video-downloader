Video Downloader —— 使用说明
================================

一、启动
    双击 VideoDownloader.exe。

    第一次启动会在本目录自动生成 .env（从同目录的 .env.example 复制），
    里面的值都是空的，需要你自己填写才生效。程序不会替你写入任何
    Cookie、Token、密码或 API Key。

二、目录说明

    VideoDownloader.exe     主程序
    _internal\              程序运行库（不要删除、不要单独移动）
    .env                    你的配置：代理、Cookie 路径等
    .env.example            配置模板
    secrets\                Instagram 登录态存放处（首次使用请自行创建该目录）
      cookies.txt           浏览器导出的 Netscape 格式 cookies.txt
    tools\ffmpeg\bin\       ffmpeg.exe 与 ffprobe.exe（音视频合并必需）
    chrome-extension\       Chrome 登录助手扩展（可选，见第六节）
    native_host\            登录助手宿主程序，Chrome 会启动它（不要删除、不要单独移动）
    downloads\              下载的视频与元数据，以及 downloads.db
    logs\downloader.log     运行日志（出问题时请看这里）

    如果本目录不可写（例如放在 C:\Program Files），程序会自动改用
    %LOCALAPPDATA%\VideoDownloader\ 存放 downloads、logs 和数据库；
    secrets\cookies.txt 也要放到那个目录下面。

三、代理（访问 YouTube 需要）

    编辑 .env：
        HTTP_PROXY=http://127.0.0.1:8090
        HTTPS_PROXY=http://127.0.0.1:8090
    代理没开、地址不对时会提示「代理连接失败」，不会静默失败。
    B站走直连，不受代理影响。

四、Instagram 登录态

    Instagram 的公开帖子只对已登录会话返回数据。请用浏览器扩展
    （例如 Get cookies.txt LOCALLY）导出 Netscape 格式的 cookies.txt，
    放到 secrets\cookies.txt。程序只读取它，从不显示、也从不打包它。
    登录态失效时会提示「Instagram 登录状态已失效，请重新更新 Cookie。」

五、ffmpeg

    需要 tools\ffmpeg\bin\ffmpeg.exe 与 ffprobe.exe。缺失时下载 B站高清
    （音视频分离）会提示「未找到 ffmpeg」。也可以在 .env 里用
    FFMPEG_PATH 指向你自己安装的 ffmpeg。

六、Chrome 登录助手（可选）

    默认浏览器是 Chrome / Edge 时，它们用 App-Bound Encryption 保护 Cookie，
    外部程序无法解密，所以本程序带一个登录助手：由 Chrome 自己解密，再把会话
    交给本机程序。扩展只申请读取 bilibili.com 与 instagram.com 的 Cookie，
    其他网站它读不到；会话只在本机进程之间传递，不会上传。

    一次性安装（在软件里点「Chrome 登录助手 → 安装 / 更新登录助手」会带你走完）：

        1. 程序把宿主程序登记到「当前用户」的注册表（不需要管理员权限）；
        2. 在打开的 chrome://extensions 页面右上角打开「开发者模式」；
        3. 点「加载已解压的扩展程序」，选择本目录下的 chrome-extension 文件夹；
        4. 登录平台后点浏览器工具栏上的扩展图标，选平台并点
           「发送到 Video Downloader」。

    扩展只需要安装一次。本目录不可写时（例如装在 C:\Program Files），程序会
    把宿主复制到 %LOCALAPPDATA%\VideoDownloader\native_host\ 再登记，因为
    Chrome 启动的宿主需要能写入自己的配置文件。

七、卸载

    直接删除整个文件夹即可。如果你装过 Chrome 登录助手，请先在软件里点
    「卸载登录助手」；它会删除注册表里属于本程序的那一项，以及上面那个
    宿主副本。你的 .env、secrets\、downloads\ 与 logs\ 永远不会被删。
