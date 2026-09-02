package com.liferadio.sync.ui.screens

import android.content.Intent
import android.content.pm.PackageManager
import android.provider.Settings
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.background
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.liferadio.sync.data.model.CentralHealthInfo
import com.liferadio.sync.data.model.CentralStepDevice
import com.liferadio.sync.data.remote.CentralStepDeviceSelector
import com.liferadio.sync.data.model.SyncStatus
import com.liferadio.sync.R
import com.liferadio.sync.ui.theme.Danger
import com.liferadio.sync.ui.theme.Info
import com.liferadio.sync.ui.theme.Success
import com.liferadio.sync.ui.theme.Warning
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * 读取应用自身版本号，UI 上始终显示真实版本
 */
private fun getAppVersion(context: android.content.Context): String {
    return try {
        val info = context.packageManager.getPackageInfo(context.packageName, 0)
        val versionName = info.versionName ?: "?"
        val versionCode = if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.P) {
            info.longVersionCode
        } else {
            @Suppress("DEPRECATION") info.versionCode.toLong()
        }
        "v$versionName($versionCode)"
    } catch (e: PackageManager.NameNotFoundException) {
        "v?"
    }
}

private val healthZone: ZoneId = ZoneId.of("Asia/Shanghai")
private val healthTimeFormatter: DateTimeFormatter = DateTimeFormatter.ofPattern("HH:mm")

private fun String?.timeLabel(): String = this?.let {
    runCatching { Instant.parse(it).atZone(healthZone).format(healthTimeFormatter) }.getOrNull()
} ?: "--:--"

private fun Long?.durationLabel(): String {
    val seconds = this ?: return "--"
    return "${seconds / 3600}小时${(seconds % 3600) / 60}分"
}

private fun List<com.liferadio.sync.data.model.CentralHealthDevice>.deviceNames(): String =
    joinToString("、") { it.displayName }

private fun CentralHealthInfo?.sleepHeadline(): String = when (this?.sleep?.status) {
    "final" -> "估算睡眠区间：${sleep.estimatedStart.timeLabel()} → ${sleep.estimatedEnd.timeLabel()}"
    "estimating" -> "估算睡眠区间：仍在估算"
    "insufficient_data" -> "估算睡眠区间：证据不足"
    else -> "估算睡眠区间：等待中央结果"
}

private fun CentralHealthInfo?.sleepStatusLabel(): String = when (this?.sleep?.status) {
    "estimating" -> "仍在估算，等待醒后使用证据"
    "insufficient_data" -> "证据不足，未生成睡眠区间"
    else -> "暂无法读取中央健康信息"
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MainScreen(
    viewModel: MainViewModel = viewModel()
) {
    val uiState by viewModel.uiState.collectAsState()
    val context = LocalContext.current
    val appVersion = remember { getAppVersion(context) }

    var selectedTab by remember { mutableIntStateOf(0) }
    val notificationPermissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { }

    LaunchedEffect(Unit) {
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.TIRAMISU &&
            context.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            notificationPermissionLauncher.launch(android.Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("Life Link", fontWeight = FontWeight.Bold)
                        Spacer(Modifier.width(8.dp))
                        Surface(
                            color = MaterialTheme.colorScheme.primaryContainer,
                            shape = RoundedCornerShape(8.dp)
                        ) {
                            Text(
                                appVersion,
                                modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp),
                                color = MaterialTheme.colorScheme.onPrimaryContainer,
                                style = MaterialTheme.typography.labelSmall,
                                fontWeight = FontWeight.Medium
                            )
                        }
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.background,
                    titleContentColor = MaterialTheme.colorScheme.onBackground
                )
            )
        },
        bottomBar = {
            NavigationBar(containerColor = MaterialTheme.colorScheme.surface, tonalElevation = 0.dp) {
                NavigationBarItem(
                    selected = selectedTab == 0,
                    onClick = { selectedTab = 0 },
                    icon = { Icon(painterResource(R.drawable.ic_lucide_bell), contentDescription = null) },
                    label = { Text("事件") }
                )
                NavigationBarItem(
                    selected = selectedTab == 1,
                    onClick = { selectedTab = 1 },
                    icon = { Icon(painterResource(R.drawable.ic_lucide_target), contentDescription = null) },
                    label = { Text("心愿") }
                )
                NavigationBarItem(
                    selected = selectedTab == 2,
                    onClick = { selectedTab = 2 },
                    icon = { Icon(painterResource(R.drawable.ic_lucide_chart_bar), contentDescription = null) },
                    label = { Text("数据") }
                )
                NavigationBarItem(
                    selected = selectedTab == 3,
                    onClick = { selectedTab = 3 },
                    icon = { Icon(painterResource(R.drawable.ic_lucide_settings_2), contentDescription = null) },
                    label = { Text("设置") }
                )
            }
        }
    ) { padding ->
        if (uiState.showArchivedWishes) {
            ArchivedWishesScreen(uiState = uiState, viewModel = viewModel, modifier = Modifier.padding(padding))
        } else when (selectedTab) {
            0 -> EventListScreen(uiState, viewModel, Modifier.padding(padding))
            1 -> WishesScreen(uiState, viewModel, Modifier.padding(padding))
            2 -> DataTab(uiState, viewModel, Modifier.padding(padding))
            3 -> SettingsTab(uiState, viewModel, Modifier.padding(padding))
        }
    }
    // Exactly one root-mounted instance serves creation, active editing, and history editing.
    WishEditorDialogs(uiState, viewModel)
}


@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DataSyncCard(uiState: UiState, onSync: () -> Unit) {
    val status = uiState.syncStatus
    val pendingCount = uiState.pendingEvents
    val syncInProgress = uiState.syncInProgress || status.isSyncing
    val cumulativeConfirmed = uiState.syncCumulativeConfirmed
    val cumulativeRemaining = uiState.syncCumulativeRemaining
    val hasPending = pendingCount > 0
    val neverSynced = status.lastSyncTime == null && status.lastSyncResult == null

    val cardColor = when {
        status.errorMessage != null -> MaterialTheme.colorScheme.errorContainer
        syncInProgress -> MaterialTheme.colorScheme.surfaceVariant
        hasPending -> MaterialTheme.colorScheme.secondaryContainer
        neverSynced -> MaterialTheme.colorScheme.surface
        else -> MaterialTheme.colorScheme.primaryContainer
    }
    val iconType = when {
        status.errorMessage != null -> Icons.Default.Error
        syncInProgress -> Icons.Default.Sync
        hasPending -> Icons.Default.CloudUpload
        neverSynced -> Icons.Default.CloudUpload
        else -> Icons.Default.CheckCircle
    }
    val iconTint = when {
        status.errorMessage != null -> Danger
        syncInProgress -> Info
        hasPending -> Warning
        neverSynced -> MaterialTheme.colorScheme.onSurfaceVariant
        else -> Success
    }
    val statusText = when {
        syncInProgress && cumulativeConfirmed > 0 ->
            "同步中（${cumulativeConfirmed} 条）"
        syncInProgress -> "正在同步..."
        status.errorMessage != null -> "同步失败"
        hasPending -> "$pendingCount 条等待同步"
        neverSynced -> "尚未同步"
        else -> "同步完成"
    }

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = cardColor),
        border = androidx.compose.foundation.BorderStroke(1.dp, iconTint.copy(alpha = 0.5f))
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(iconType, contentDescription = null, tint = iconTint)
                Spacer(modifier = Modifier.width(12.dp))
                Column(modifier = Modifier.weight(1f)) {
                    Text("数据同步", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                    Text(statusText, color = iconTint, fontWeight = FontWeight.Medium)
                }
            }
            Spacer(modifier = Modifier.height(8.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = if (syncInProgress && cumulativeConfirmed > 0) {
                        "已确认 $cumulativeConfirmed 条，队列剩余 $cumulativeRemaining 条"
                    } else {
                        status.lastSyncResult
                            ?: status.lastSyncTime?.let {
                                "上次同步: ${java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.getDefault()).format(java.util.Date(it))}"
                            }
                            ?: "等待操作"
                    },
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.weight(1f)
                )
                Spacer(modifier = Modifier.width(12.dp))
                Button(onClick = onSync, enabled = !syncInProgress) {
                    Icon(Icons.Default.Sync, contentDescription = null, modifier = Modifier.size(18.dp))
                    Spacer(modifier = Modifier.width(6.dp))
                    Text(if (syncInProgress) "同步中" else "立即同步")
                }
            }
        }
    }
}

// ==================== 事件与心愿 ====================

@Composable
private fun EventListScreen(
    uiState: UiState,
    viewModel: MainViewModel,
    modifier: Modifier = Modifier
) {
    LaunchedEffect(Unit) { viewModel.refreshTimeline() }
    val timelineGroups = groupTodayAndYesterdayTimelineEvents(
        uiState.timelineEvents,
        uiState.sharedDayStartHour,
        Instant.now()
    )
    LazyColumn(
        modifier = modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        item {
            Row(verticalAlignment = Alignment.Top) {
                Column(modifier = Modifier.weight(1f)) {
                    Text("事件", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
                    Text("今天和昨天（按设置中的业务日）· 来自中央服务的事件记录",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                IconButton(onClick = { viewModel.refreshTimeline() }, enabled = !uiState.timelineLoading) {
                    Icon(Icons.Default.Refresh, contentDescription = "刷新事件")
                }
            }
        }
        if (uiState.timelineLoading) {
            item { Box(modifier = Modifier.fillMaxWidth().padding(32.dp), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(modifier = Modifier.size(24.dp), strokeWidth = 2.dp) } }
        } else if (!timelineGroups.isEmpty) {
            items(timelineGroups.today, key = { it.timelineEventId }) { event ->
                TimelineEventCard(event, uiState.sharedDayStartHour)
            }
            if (timelineGroups.showDivider) {
                item {
                    Divider(
                        modifier = Modifier.padding(vertical = 8.dp),
                        color = MaterialTheme.colorScheme.outlineVariant
                    )
                }
            }
            items(timelineGroups.yesterday, key = { it.timelineEventId }) { event ->
                TimelineEventCard(event, uiState.sharedDayStartHour)
            }
        } else {
            item { EmptyEventCard("今天和昨天没有事件", "新事件会在同步后出现在这里") }
        }
        if (uiState.timelineCacheOnly) {
            item { Text("当前离线中（只读缓存）", style = MaterialTheme.typography.labelSmall, color = Warning) }
        }
    }
}

@Composable
private fun WishesScreen(
    uiState: UiState,
    viewModel: MainViewModel,
    modifier: Modifier = Modifier
) {
    LazyColumn(
        modifier = modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        item {
            Text("心愿", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Text("记录短期目标，并在每天结束前更新进度",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        item { WishesCard(uiState, viewModel) }
    }
}

@Composable
private fun ArchivedWishesScreen(
    uiState: UiState,
    viewModel: MainViewModel,
    modifier: Modifier = Modifier
) {
    BackHandler(enabled = true) { viewModel.dismissHistory() }

    LazyColumn(
        modifier = modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically
            ) {
                IconButton(onClick = { viewModel.dismissHistory() }) {
                    Icon(Icons.Default.ArrowBack, "返回")
                }
                Text("往期心愿", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold,
                    modifier = Modifier.weight(1f))
            }
        }

        // Archived wishes
        if (uiState.archivedWishes.isNotEmpty()) {
            items(uiState.archivedWishes) { wish ->
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(modifier = Modifier.padding(12.dp)) {
                        Text(wish.text, fontWeight = FontWeight.Medium)
                        Text("${wish.startsOn} → ${wish.endsOn}  ·  完成 ${wish.completedDays}/${wish.durationDays} 天  ·  ${wishStatusLabel(wish.status)}",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f))
                        TextButton(
                            onClick = { viewModel.showWishEditor(wish.wishId) },
                            enabled = !uiState.wishesCacheOnly
                        ) { Text("编辑或删除") }
                    }
                }
            }
        } else {
            item {
                Card(modifier = Modifier.fillMaxWidth()) {
                    Text("无往期心愿", modifier = Modifier.padding(16.dp),
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.4f))
                }
            }
        }
        if (uiState.wishesCacheOnly && uiState.archivedWishes.isNotEmpty()) {
            item {
                Text("（离线缓存，只读）", style = MaterialTheme.typography.labelSmall,
                    color = Warning)
            }
        }

    }
}

@Composable
private fun EmptyEventCard(message: String, detail: String) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(20.dp)) {
            Text(message, fontWeight = FontWeight.Medium)
            Text(detail, style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

private fun wishStatusLabel(status: String): String = when (status) {
    "archived" -> "已归档"
    "cancelled" -> "已取消"
    else -> status
}

@Composable
private fun TimelineEventCard(
    event: com.liferadio.sync.data.model.TimelineEvent,
    dayStartHour: Int
) {
    val iconStyle = timelineIconStyle(event)
    val emphasisBorder = when (event.importance) {
        "high" -> androidx.compose.foundation.BorderStroke(2.dp, Warning)
        "normal" -> androidx.compose.foundation.BorderStroke(1.dp, MaterialTheme.colorScheme.primary)
        else -> null
    }
    var expanded by rememberSaveable(event.timelineEventId) { mutableStateOf(false) }
    Card(modifier = Modifier.fillMaxWidth(), border = emphasisBorder) {
        Row(modifier = Modifier.padding(12.dp), verticalAlignment = Alignment.Top) {
            Box(
                modifier = Modifier
                    .size(40.dp)
                    .background(iconStyle.background, CircleShape),
                contentAlignment = Alignment.Center
            ) {
                Icon(
                    painter = painterResource(iconStyle.iconRes),
                    contentDescription = null,
                    tint = Color.White,
                    modifier = Modifier.size(22.dp)
                )
            }
            Spacer(Modifier.width(12.dp))
            Column(modifier = Modifier.weight(1f)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(event.title, style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                    if (event.importance == "high") {
                        Surface(color = MaterialTheme.colorScheme.secondaryContainer, shape = RoundedCornerShape(10.dp)) {
                            Text("重要", modifier = Modifier.padding(horizontal = 8.dp, vertical = 3.dp),
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSecondaryContainer)
                        }
                    }
                }
                val cardDetail = timelineCardDetail(event)
                if (!cardDetail.isNullOrBlank()) {
                    val shownDetail = if (expanded || cardDetail.length <= 140) cardDetail else cardDetail.take(140) + "…"
                    Text(eventDetailAnnotated(shownDetail), style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f))
                    if (cardDetail.length > 140) TextButton(onClick = { expanded = !expanded }) { Text(if (expanded) "收起" else "展开详情") }
                }
                event.delivery?.let { Text(deliveryLabel(event), style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant) }
                Text(formatTimelineTime(event.occurredAt, dayStartHour), style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

private data class TimelineIconStyle(val iconRes: Int, val background: Color)

private fun timelineIconStyle(event: com.liferadio.sync.data.model.TimelineEvent): TimelineIconStyle = when {
    event.eventKey == "system.device_usage_milestone" ->
        TimelineIconStyle(R.drawable.ic_lucide_monitor, Color(0xFF3B82F6))
    event.eventKey == "system.blacklist_usage_milestone" ->
        TimelineIconStyle(R.drawable.ic_lucide_shield_alert, Color(0xFFEF4444))
    event.eventKey == "system.location_stay_milestone" ->
        TimelineIconStyle(R.drawable.ic_lucide_map_pin, Color(0xFF8B5CF6))
    event.eventKey == "system.activity_duration_milestone" ->
        TimelineIconStyle(R.drawable.ic_lucide_activity, Color(0xFF10B981))
    event.eventKey == "system.late_online_check" ->
        TimelineIconStyle(R.drawable.ic_lucide_moon, Color(0xFF6366F1))
    event.eventKey == "report.morning" ->
        TimelineIconStyle(R.drawable.ic_lucide_sunrise, Color(0xFFF97316))
    event.eventKey == "report.evening" ->
        TimelineIconStyle(R.drawable.ic_lucide_sunset, Color(0xFFEC4899))
    event.eventKey == "report.periodic" ->
        TimelineIconStyle(R.drawable.ic_lucide_clock, Color(0xFF14B8A6))
    event.eventKey.startsWith("wish.") || event.wishId != null ->
        TimelineIconStyle(R.drawable.ic_lucide_target, Color(0xFFF59E0B))
    event.category == "system" ->
        TimelineIconStyle(R.drawable.ic_lucide_settings, Color(0xFF6B7280))
    else -> TimelineIconStyle(R.drawable.ic_lucide_zap, Color(0xFF3B82F6))
}

private fun eventDetailAnnotated(detail: String) = buildAnnotatedString {
    val matches = Regex("\\d+小时(?:\\d+分钟)?|\\d+分钟").findAll(detail).toList()
    var cursor = 0
    matches.forEach { match ->
        append(detail.substring(cursor, match.range.first))
        withStyle(SpanStyle(color = Color(0xFFDC2626), fontWeight = FontWeight.Bold)) { append(match.value) }
        cursor = match.range.last + 1
    }
    append(detail.substring(cursor))
}

internal fun timelineCardDetail(event: com.liferadio.sync.data.model.TimelineEvent): String? =
    if (event.delivery != null && event.eventKey.startsWith("report.")) null else event.detail

internal fun deliveryLabel(event: com.liferadio.sync.data.model.TimelineEvent): String {
    val target = event.delivery?.targetDisplayName ?: "AI"
    val name = when (event.eventKey) { "report.morning" -> "今日早报"; "report.evening" -> "今日晚报"; else -> "定时总结" }
    return when (event.delivery?.state) { "pending" -> "${name}发送至 $target。正在发送…"; "sent" -> "${name}发送至 $target。已成功发送！"; "failed" -> "${name}发送至 $target。发送失败。"; else -> "${name}已准备就绪。等待 $target 接入。" }
}

internal fun formatTimelineTime(
    utcStr: String,
    dayStartHour: Int,
    now: Instant = Instant.now()
): String {
    val instant = runCatching { java.time.Instant.parse(utcStr) }.getOrNull() ?: return utcStr
    val zdt = instant.atZone(java.time.ZoneId.of("Asia/Shanghai"))
    val today = com.liferadio.sync.data.model.EventBusinessDay.at(dayStartHour, now)
    val prefix = when (com.liferadio.sync.data.model.EventBusinessDay.at(dayStartHour, instant)) {
        today -> "今天"
        today.minusDays(1) -> "昨天"
        else -> java.time.format.DateTimeFormatter.ofPattern("MM-dd").format(zdt)
    }
    return "$prefix ${java.time.format.DateTimeFormatter.ofPattern("HH:mm").format(zdt)}"
}

// ==================== 心愿卡片 ====================

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun WishesCard(uiState: UiState, viewModel: MainViewModel) {
    WishEditorDialogs(uiState, viewModel, showCard = true, showDialogs = false)
}

@Composable
@OptIn(ExperimentalMaterial3Api::class)
private fun WishEditorDialogs(
    uiState: UiState,
    viewModel: MainViewModel,
    showCard: Boolean = false,
    showDialogs: Boolean = true,
    modifier: Modifier = Modifier
) {
    val wishes = uiState.wishes
    val loading = uiState.wishesLoading
    val error = uiState.wishesError
    val showCreate = uiState.showWishCreateDialog
    val isEditing = uiState.editingWishId != null

    if (showCard) Card(
        modifier = modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text("当前心愿 (${wishes.size}/3)", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                Row {
                    if (wishes.size < 3 && uiState.centralTokenConfigured && !uiState.wishesCacheOnly) {
                        IconButton(
                            onClick = { viewModel.showCreateWishDialog() },
                            modifier = Modifier.size(36.dp)
                        ) {
                            Icon(Icons.Default.Add, contentDescription = "创建心愿",
                                tint = MaterialTheme.colorScheme.primary)
                        }
                    }
                    IconButton(
                        onClick = { viewModel.refreshWishes() },
                        modifier = Modifier.size(36.dp),
                        enabled = !loading
                    ) {
                        Icon(Icons.Default.Refresh, contentDescription = "刷新",
                            modifier = Modifier.size(20.dp))
                    }
                }
            }

            if (loading) {
                Box(modifier = Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator(modifier = Modifier.size(24.dp), strokeWidth = 2.dp)
                }
            } else if (error.isNotEmpty() && wishes.isEmpty()) {
                Text(error, style = MaterialTheme.typography.bodySmall,
                    color = Danger, modifier = Modifier.padding(top = 8.dp))
            } else if (wishes.isEmpty() && !uiState.centralTokenConfigured) {
                Text("绑定中央服务后可创建心愿",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.4f),
                    modifier = Modifier.padding(top = 8.dp))
            } else if (wishes.isEmpty()) {
                Text("还没有进行中的心愿，点击右上角 + 创建",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.4f),
                    modifier = Modifier.padding(top = 8.dp))
            }

            if (wishes.isNotEmpty()) {
                Spacer(Modifier.height(8.dp))
                wishes.forEachIndexed { index, wish ->
                    if (index > 0) Spacer(Modifier.height(6.dp))
                    WishRow(wish, uiState.wishDayAssessing, uiState.wishesCacheOnly,
                        uiState.wishTriggerMap[wish.wishId]?.enabled == true,
                        !uiState.wishesCacheOnly && !uiState.triggersOffline &&
                            !uiState.triggerCatalogsOffline && uiState.triggerCatalog.isNotEmpty() &&
                            wish.wishId !in uiState.triggerConflictWishIds,
                        wish.wishId in uiState.triggerConflictWishIds,
                        uiState.wishCompletingId == wish.wishId,
                        viewModel)
                }
            }

            // Cache-only indicator
            if (uiState.wishesCacheOnly && wishes.isNotEmpty()) {
                Spacer(Modifier.height(4.dp))
                Text("（离线缓存，无法刷新）", style = MaterialTheme.typography.labelSmall,
                    color = Warning)
            }

            // Archive entry
            Spacer(Modifier.height(4.dp))
            TextButton(
                onClick = { viewModel.showHistoryAndTimeline() },
                modifier = Modifier.fillMaxWidth()
            ) {
                Text("查看往期心愿", style = MaterialTheme.typography.bodySmall)
            }
        }
    }

    // One shared template: create starts blank; edit reuses every field with immutable duration.
    if (showDialogs && showCreate) {
        AlertDialog(
            onDismissRequest = { viewModel.dismissWishDialog() },
            title = { Text(if (isEditing) "编辑心愿" else "新心愿") },
            text = {
                Column {
                    OutlinedTextField(
                        value = uiState.wishCreateText,
                        onValueChange = {
                            viewModel.updateWishCreateText(it)
                        },
                        label = { Text("心愿内容（1-30字）") },
                        /*label = { Text("心愿内容（1-30字）") },*/
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                        enabled = !uiState.wishCreateSending
                    )
                    Spacer(Modifier.height(12.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("天数：", style = MaterialTheme.typography.bodyMedium)
                        FilterChip(
                            selected = uiState.wishCreateDuration == 3,
                            onClick = { viewModel.setWishCreateDuration(3) },
                            label = { Text("3 天") },
                            modifier = Modifier.padding(end = 8.dp),
                            enabled = WishEditorPolicy.isDurationMutable(uiState.editingWishId) && !uiState.wishCreateSending
                        )
                        FilterChip(
                            selected = uiState.wishCreateDuration == 7,
                            onClick = { viewModel.setWishCreateDuration(7) },
                            label = { Text("7 天") },
                            enabled = WishEditorPolicy.isDurationMutable(uiState.editingWishId) && !uiState.wishCreateSending
                        )
                    }
                    if (uiState.wishCreateError.isNotEmpty()) {
                        Spacer(Modifier.height(8.dp))
                        Text(uiState.wishCreateError, color = Danger,
                            style = MaterialTheme.typography.bodySmall)
                    }

                    // Archived/cancelled records retain their historical reminder but cannot reconfigure it.
                    val reminderEditable = !isEditing || uiState.editingWishCanEditReminder
                    if (reminderEditable) {
                    Spacer(Modifier.height(12.dp))
                    Text("提醒设置", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
                    Spacer(Modifier.height(4.dp))
                    val availableTypes = uiState.triggerCatalog.map { it.triggerType }.toSet()
                    val triggerTypes = listOf(
                        null to "不启用提醒",
                        "blacklist_usage_milestone" to "黑名单累计用量",
                        "device_usage_milestone" to "本设备累计用量",
                        "late_usage_milestone" to "晚间使用提醒",
                        "scheduled_reminder" to "定时提醒"
                    ).filter { (type, _) -> type == null || type in availableTypes }
                    triggerTypes.forEach { (type, label) ->
                        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(vertical = 2.dp)) {
                            RadioButton(
                                selected = uiState.wishCreateTriggerType == type,
                                onClick = { viewModel.setWishCreateTrigger(type) },
                                enabled = !uiState.wishCreateSending && !uiState.triggerCatalogsOffline && !uiState.triggersOffline
                            )
                            Text(label, style = MaterialTheme.typography.bodySmall)
                        }
                    }
                    // Basic param fields for selected type
                    val selType = uiState.wishCreateTriggerType
                    if (selType == "blacklist_usage_milestone") {
                        Spacer(Modifier.height(4.dp))
                        Text("范围：全平台", style = MaterialTheme.typography.bodySmall)
                    }
                    if (selType == "late_usage_milestone") {
                        Spacer(Modifier.height(4.dp))
                        OutlinedTextField(
                            value = uiState.wishCreateTriggerParams["start_local_time"] ?: "23:00",
                            onValueChange = { viewModel.setWishCreateTriggerParam("start_local_time", it.take(5)) },
                            label = { Text("开始时间（HH:mm）") },
                            singleLine = true,
                            enabled = !uiState.wishCreateSending,
                            modifier = Modifier.fillMaxWidth()
                        )
                    }
                    if (selType == "scheduled_reminder") {
                        OutlinedTextField(value = uiState.wishCreateTriggerParams["reminder_local_time"] ?: "22:30",
                            onValueChange = { viewModel.setWishCreateTriggerParam("reminder_local_time", it.take(5)) }, label = { Text("提醒时间（HH:mm）") }, singleLine = true, modifier = Modifier.fillMaxWidth())
                    }
                    if (selType != null && selType != "scheduled_reminder") {
                        TriggerIntervalChoices(
                            selected = uiState.wishCreateTriggerInterval,
                            allowed = uiState.triggerCatalog.firstOrNull { it.triggerType == selType }
                                ?.intervalMinutes?.allowedValues.orEmpty(),
                            enabled = !uiState.wishCreateSending,
                            onSelect = viewModel::setWishCreateTriggerInterval
                        )
                    }
                    if ((uiState.triggerCatalogsOffline || uiState.triggersOffline) && selType != null) {
                        Text("当前离线中，提醒设置暂不可写入", color = Warning,
                            style = MaterialTheme.typography.bodySmall)
                    }
                    }
                }
            },
            confirmButton = {
                Button(
                    onClick = { viewModel.saveWishEditor() },
                    enabled = uiState.wishCreateText.trim().isNotEmpty() && !uiState.wishCreateSending &&
                        (isEditing && !uiState.editingWishCanEditReminder ||
                            (isTriggerSelectionValid(uiState.wishCreateTriggerType, uiState.wishCreateTriggerParams) &&
                                (uiState.wishCreateTriggerType == null || (!uiState.triggerCatalogsOffline && !uiState.triggersOffline))))
                ) {
                    if (uiState.wishCreateSending) {
                        CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
                        Spacer(Modifier.width(8.dp))
                    }
                    Text(if (isEditing) "保存" else "创建")
                }
            },
            dismissButton = {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    TextButton(onClick = { viewModel.dismissWishDialog() }) { Text("取消") }
                    if (isEditing) {
                        TextButton(onClick = { viewModel.showWishDelete(uiState.editingWishId!!) },
                            enabled = !uiState.wishCreateSending) {
                            Text("删除心愿", color = Danger)
                        }
                    }
                }
            }
        )
    }

    // One explicit confirmation is required before the central permanent delete.
    val deleteId = uiState.showWishDeleteConfirm
    if (showDialogs && deleteId != null) {
        AlertDialog(
            onDismissRequest = { viewModel.dismissWishDelete() },
            title = { Text("删除心愿") },
            text = { Text("确定永久删除这条心愿吗？它的日期记录和提醒设置将一并删除，无法恢复。") },
            confirmButton = {
                Button(onClick = { viewModel.deleteWish(deleteId) }, colors = ButtonDefaults.buttonColors(
                    containerColor = Danger
                )) { Text("确认删除") }
            },
            dismissButton = {
                TextButton(onClick = { viewModel.dismissWishDelete() }) { Text("取消") }
            }
        )
    }

    val completeId = uiState.showWishCompleteConfirm
    if (showDialogs && completeId != null) {
        val wish = uiState.wishes.firstOrNull { it.wishId == completeId }
        val missingDates = wish?.wishDays.orEmpty().filter { it.evaluation == null }.map { formatWishDayFullLabel(it.businessDate) }
        AlertDialog(
            onDismissRequest = { if (uiState.wishCompletingId == null) viewModel.dismissWishComplete() },
            icon = { Icon(Icons.Default.CheckCircle, contentDescription = null, tint = Success) },
            title = { Text("完结心愿") },
            text = {
                Text(if (missingDates.isNotEmpty()) {
                    "${missingDates.joinToString("、")} 日期结果还未填写，请先填写后再完结心愿。"
                } else {
                    "确认完结“${wish?.text.orEmpty()}”吗？完结后将移入往期心愿，并生成结果事件。"
                })
            },
            confirmButton = {
                if (missingDates.isEmpty()) {
                    Button(
                        onClick = { viewModel.completeWish(completeId) },
                        enabled = uiState.wishCompletingId == null
                    ) { Text(if (uiState.wishCompletingId == null) "确认完结" else "正在完结…") }
                } else {
                    TextButton(onClick = { viewModel.dismissWishComplete() }) { Text("知道了") }
                }
            },
            dismissButton = {
                if (missingDates.isEmpty()) {
                    TextButton(onClick = { viewModel.dismissWishComplete() }, enabled = uiState.wishCompletingId == null) { Text("取消") }
                }
            }
        )
    }

    // Trigger dialog
    val triggerWishId = uiState.showTriggerDialogForWish
    if (showDialogs && triggerWishId != null) {
        val existing = uiState.wishTriggerMap[triggerWishId]
        AlertDialog(
            onDismissRequest = { viewModel.dismissTriggerDialog() },
            title = { Text(if (existing?.enabled == true) "更换提醒" else "设置提醒") },
            text = {
                Column {
                    val availableTypes = uiState.triggerCatalog.map { it.triggerType }.toSet()
                    val types = listOf(
                        "blacklist_usage_milestone" to "黑名单累计用量",
                        "device_usage_milestone" to "本设备累计用量",
                        "late_usage_milestone" to "晚间使用提醒",
                        "scheduled_reminder" to "定时提醒"
                    ).filter { it.first in availableTypes }
                    types.forEach { (type, label) ->
                        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(vertical = 2.dp)) {
                            RadioButton(
                                selected = uiState.triggerDialogType == type,
                                onClick = { viewModel.setTriggerDialogType(type) },
                                enabled = !uiState.triggerDialogSending
                            )
                            Text(label, style = MaterialTheme.typography.bodySmall)
                        }
                    }
                    val selType = uiState.triggerDialogType
                    if (selType == "blacklist_usage_milestone") {
                        Spacer(Modifier.height(4.dp))
                        Text("范围：全平台", style = MaterialTheme.typography.bodySmall)
                    }
                    if (selType == "late_usage_milestone") {
                        Spacer(Modifier.height(4.dp))
                        OutlinedTextField(
                            value = uiState.triggerDialogParams["start_local_time"] ?: "23:00",
                            onValueChange = { viewModel.setTriggerDialogParam("start_local_time", it.take(5)) },
                            label = { Text("开始时间（HH:mm）") },
                            singleLine = true,
                            enabled = !uiState.triggerDialogSending,
                            modifier = Modifier.fillMaxWidth()
                        )
                    }
                    if (selType == "scheduled_reminder") {
                        OutlinedTextField(value = uiState.triggerDialogParams["reminder_local_time"] ?: "22:30",
                            onValueChange = { viewModel.setTriggerDialogParam("reminder_local_time", it.take(5)) }, label = { Text("提醒时间（HH:mm）") }, singleLine = true, modifier = Modifier.fillMaxWidth())
                    }
                    if (selType != null && selType != "scheduled_reminder") {
                        TriggerIntervalChoices(
                            selected = uiState.triggerDialogInterval,
                            allowed = uiState.triggerCatalog.firstOrNull { it.triggerType == selType }
                                ?.intervalMinutes?.allowedValues.orEmpty(),
                            enabled = !uiState.triggerDialogSending,
                            onSelect = viewModel::setTriggerDialogInterval
                        )
                    }
                    if (existing?.enabled == true) {
                        Spacer(Modifier.height(8.dp))
                        TextButton(
                            onClick = { viewModel.removeTriggerFromWish(triggerWishId) },
                            enabled = !uiState.triggerDialogSending
                        ) {
                            Text("关闭提醒", color = Danger)
                        }
                    }
                    if (uiState.triggerDialogError.isNotEmpty()) {
                        Spacer(Modifier.height(4.dp))
                        Text(uiState.triggerDialogError, color = Danger, style = MaterialTheme.typography.bodySmall)
                    }
                }
            },
            confirmButton = {
                Button(
                    onClick = { viewModel.saveTriggerForWish() },
                    enabled = uiState.triggerDialogType != null && !uiState.triggerDialogSending &&
                        isTriggerSelectionValid(uiState.triggerDialogType, uiState.triggerDialogParams)
                ) { Text("保存") }
            },
            dismissButton = {
                TextButton(onClick = { viewModel.dismissTriggerDialog() }) { Text("取消") }
            }
        )
    }
}

private fun isTriggerSelectionValid(type: String?, params: Map<String, String>): Boolean =
    type !in setOf("late_usage_milestone", "scheduled_reminder") || Regex("(?:[01][0-9]|2[0-3]):[0-5][0-9]")
        .matches(params[if (type == "scheduled_reminder") "reminder_local_time" else "start_local_time"] ?: "23:00")

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun TriggerIntervalChoices(
    selected: Int,
    allowed: List<Int>,
    enabled: Boolean,
    onSelect: (Int) -> Unit
) {
    val values = allowed.ifEmpty { listOf(15, 30, 60, 120) }
    Spacer(Modifier.height(6.dp))
    Text("提醒周期", style = MaterialTheme.typography.bodySmall)
    Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        values.forEach { minutes ->
            FilterChip(
                selected = selected == minutes,
                onClick = { onSelect(minutes) },
                label = { Text(if (minutes < 60) "${minutes}分" else "${minutes / 60}时") },
                enabled = enabled
            )
        }
    }
}

@Composable
private fun WishRow(
    wish: com.liferadio.sync.data.model.Wish,
    assessingKey: String?,
    readOnly: Boolean,
    hasTrigger: Boolean,
    triggerEditable: Boolean,
    triggerConflict: Boolean,
    isCompleting: Boolean,
    viewModel: MainViewModel
) {
    val nowBusinessDate = com.liferadio.sync.data.model.WishBusinessDay.now(wish.businessDaySnapshot)
    val days = wish.wishDays
    Column {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(wish.text, fontWeight = FontWeight.Medium, modifier = Modifier.weight(1f))
            Row {
                if (wish.completedDays > 0) {
                    Text(
                        "${wish.completedDays}/${wish.durationDays} 完成",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.primary
                    )
                    Spacer(Modifier.width(6.dp))
                }
                // Trigger icon
                if (hasTrigger || triggerEditable || triggerConflict) {
                    IconButton(
                        onClick = { viewModel.showTriggerDialog(wish.wishId) },
                        enabled = triggerEditable,
                        modifier = Modifier.size(24.dp)
                    ) {
                        Icon(
                            if (hasTrigger) Icons.Default.NotificationsActive else Icons.Default.NotificationsNone,
                            contentDescription = if (triggerConflict) "提醒记录冲突" else "提醒设置",
                            modifier = Modifier.size(14.dp),
                            tint = if (triggerConflict) Danger else MaterialTheme.colorScheme.primary
                        )
                    }
                }
                IconButton(
                    onClick = { viewModel.showWishEditor(wish.wishId) },
                    enabled = !readOnly,
                    modifier = Modifier.size(28.dp)
                ) {
                    Icon(Icons.Default.Edit, contentDescription = "编辑心愿",
                        modifier = Modifier.size(16.dp),
                        tint = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.3f))
                }
            }
        }

        Spacer(Modifier.height(4.dp))

        // Day strip — compact single row with business-day-aware status
        Row(modifier = Modifier.fillMaxWidth()) {
            days.forEach { day ->
                val status = day.dayStatus(nowBusinessDate)
                val key = "${wish.wishId}:${day.businessDate}"
                val isAssessing = assessingKey == key
                val reachable = !readOnly && status != com.liferadio.sync.data.model.WishDayStatus.UNREACHED
                    && status != com.liferadio.sync.data.model.WishDayStatus.UNKNOWN
                WishDayChip(
                    status = status,
                    label = formatWishDayLabel(day.businessDate),
                    isAssessing = isAssessing,
                    onClick = if (reachable) {
                        { viewModel.assessWishDay(wish.wishId, day.businessDate,
                            if (day.evaluation == "completed") "not_completed" else "completed") }
                    } else null
                )
            }
        }
        if (runCatching { java.time.LocalDate.parse(wish.endsOn).isBefore(nowBusinessDate) }.getOrDefault(false)) {
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                TextButton(
                    onClick = { viewModel.showWishComplete(wish.wishId) },
                    enabled = !readOnly && !isCompleting
                ) {
                    Icon(Icons.Default.CheckCircle, contentDescription = null, modifier = Modifier.size(16.dp))
                    Spacer(Modifier.width(4.dp))
                    Text("完结心愿")
                }
            }
        }
    }
}

/**
 * Format business_date "2026-08-09" → "9日" for compact display.
 */
private fun formatWishDayLabel(businessDate: String): String {
    val parts = businessDate.split("-")
    if (parts.size != 3) return businessDate
    val day = parts[2].toIntOrNull() ?: return businessDate
    return "${day}日"
}

private fun formatWishDayFullLabel(businessDate: String): String {
    val date = runCatching { java.time.LocalDate.parse(businessDate) }.getOrNull() ?: return businessDate
    return "${date.monthValue}月${date.dayOfMonth}日"
}

/**
 * Compact rectangular chip indicating wish day status.
 */
@Composable
private fun WishDayChip(
    status: com.liferadio.sync.data.model.WishDayStatus,
    label: String,
    isAssessing: Boolean,
    onClick: (() -> Unit)?
) {
    val bgColor = when (status) {
        com.liferadio.sync.data.model.WishDayStatus.COMPLETED -> Success
        com.liferadio.sync.data.model.WishDayStatus.NOT_COMPLETED, com.liferadio.sync.data.model.WishDayStatus.PAST_PENDING -> Danger
        com.liferadio.sync.data.model.WishDayStatus.TODAY -> Info
        else -> Color(0xFFBDC3C7)
    }
    val textColor = when (status) {
        com.liferadio.sync.data.model.WishDayStatus.COMPLETED, com.liferadio.sync.data.model.WishDayStatus.NOT_COMPLETED,
        com.liferadio.sync.data.model.WishDayStatus.PAST_PENDING -> Color.White
        com.liferadio.sync.data.model.WishDayStatus.TODAY -> Color.White
        else -> Color(0xFF666666)
    }
    val statusIcon = when (status) {
        com.liferadio.sync.data.model.WishDayStatus.COMPLETED -> "\u2713"
        com.liferadio.sync.data.model.WishDayStatus.NOT_COMPLETED -> "\u2717"
        com.liferadio.sync.data.model.WishDayStatus.PAST_PENDING -> "!"
        else -> ""
    }

    Surface(
        onClick = { onClick?.invoke() },
        shape = RoundedCornerShape(4.dp),
        color = if (isAssessing) bgColor.copy(alpha = 0.5f) else bgColor.copy(alpha = 0.15f),
        enabled = onClick != null,
        modifier = Modifier.padding(end = 3.dp)
    ) {
        Column(
            modifier = Modifier.padding(horizontal = 5.dp, vertical = 2.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Text(label, style = MaterialTheme.typography.labelSmall, color = textColor, fontWeight = FontWeight.Medium)
            if (statusIcon.isNotEmpty()) {
                Text(statusIcon, style = MaterialTheme.typography.labelSmall, color = textColor)
            }
        }
    }
}

@Composable
private fun ConnectionStatusCard(uiState: UiState) {
    val tokenOk = uiState.centralTokenConfigured
    val reachable = uiState.centralReachable
    val lastChecked = uiState.centralLastCheckedAt

    val cardColor: Color
    val iconType: ImageVector
    val iconTint: Color
    val statusText: String
    val detailText: String

    when {
        !tokenOk -> {
            cardColor = Color(0xFFFFF3E0)
            iconType = Icons.Default.Warning
            iconTint = Warning
            statusText = "未绑定中央服务"
            detailText = "请粘贴 LR1 邀请码完成绑定"
        }
        reachable == null -> {
            cardColor = Color(0xFFF5F5F5)
            iconType = Icons.Default.Refresh
            iconTint = Color(0xFF888780)
            statusText = "正在检测连接…"
            detailText = uiState.centralHealthMessage.ifBlank { "等待首次检测" }
        }
        reachable -> {
            cardColor = Color(0xFFE8F5E9)
            iconType = Icons.Default.CheckCircle
            iconTint = Success
            statusText = "中央服务已连接"
            detailText = uiState.centralHealthMessage.ifBlank { "服务端正常响应" }
        }
        else -> {
            cardColor = Color(0xFFFFEBEE)
            iconType = Icons.Default.Error
            iconTint = Danger
            statusText = "中央服务不可达"
            detailText = uiState.centralHealthMessage.ifBlank { "请检查服务端是否运行" }
        }
    }

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = cardColor)
    ) {
        Row(
            modifier = Modifier.padding(16.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Icon(iconType, contentDescription = null, tint = iconTint, modifier = Modifier.size(28.dp))
            Spacer(modifier = Modifier.width(12.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(statusText, fontWeight = FontWeight.Bold)
                Text(
                    text = buildString {
                        append(detailText)
                        if (tokenOk) {
                            append(" · ")
                            append(uiState.centralBaseUrl.take(30).ifBlank { "未设地址" })
                        }
                        lastChecked?.let {
                            append("\n上次检测: ${java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.getDefault()).format(java.util.Date(it))}")
                        }
                    },
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f)
                )
            }
        }
    }
}

// ==================== 数据页 ====================

@Composable
private fun HealthInfoCard(uiState: UiState, viewModel: MainViewModel) {
    val context = LocalContext.current
    val health = uiState.healthInfo
    val hasSensor = uiState.stepCounterAvailable

    LaunchedEffect(Unit) { viewModel.refreshLocalActivity() }
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            com.liferadio.sync.service.SyncService.refreshStepPermission(context)
        }
        viewModel.refreshHealth()
    }

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.Bedtime, null, tint = MaterialTheme.colorScheme.primary)
                Spacer(Modifier.width(8.dp))
                Text("健康信息", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
            }
            Spacer(Modifier.height(12.dp))

            Text(
                health.sleepHeadline(),
                style = MaterialTheme.typography.headlineSmall,
                fontWeight = FontWeight.Bold,
                color = MaterialTheme.colorScheme.onSurface
            )
            Spacer(Modifier.height(4.dp))

            if (health?.sleep?.status == "final") {
                Text("区间跨度：${health.sleep.intervalSeconds.durationLabel()}")
                Text("睡前最后使用：${health.sleep.lastActivityDevices.deviceNames()}")
                Spacer(Modifier.height(2.dp))
                Text("醒后最早使用：${health.sleep.firstActivityDevices.deviceNames()}")
            } else {
                Text(health.sleepStatusLabel(),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.5f))
            }

            Spacer(Modifier.height(4.dp))
            health?.let { Text("统计日期：${it.date}", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.45f)) }
            if (uiState.healthInfoOffline) Text("当前离线中（显示最近一次中央结果）", color = Warning, style = MaterialTheme.typography.labelSmall)
            if (uiState.healthInfoError.isNotBlank()) Text(uiState.healthInfoError, color = Danger, style = MaterialTheme.typography.labelSmall)
            Text("估算值，仅供参考",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.35f))

            Spacer(Modifier.height(12.dp))
            Divider()
            Spacer(Modifier.height(8.dp))

            // Step count
            Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.DirectionsWalk, null,
                    tint = if (hasSensor) Success else MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.size(20.dp))
                Spacer(Modifier.width(8.dp))
                Text(
                    if (!uiState.activityRecognitionGranted) "计步器：需要“身体活动”授权"
                    else if (hasSensor && health?.steps?.devices?.isNotEmpty() == true) "步数：${health.steps.devices.size} 台设备的中央结果"
                    else if (hasSensor) "步数：暂无中央结果"
                    else "计步器：此设备不支持",
                    style = MaterialTheme.typography.bodyMedium,
                    fontWeight = FontWeight.Medium
                )
                if (!uiState.activityRecognitionGranted) {
                    Spacer(Modifier.width(8.dp))
                    TextButton(onClick = {
                        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.Q) {
                            permissionLauncher.launch(android.Manifest.permission.ACTIVITY_RECOGNITION)
                        }
                    }) { Text("授权") }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun HealthDetailsScreen(
    uiState: UiState,
    viewModel: MainViewModel,
    onBack: () -> Unit,
    modifier: Modifier = Modifier
) {
    val health = uiState.healthInfo
    val samples = uiState.stepSamples
    val hasSensor = uiState.stepCounterAvailable

    BackHandler(onBack = onBack)

    LazyColumn(
        modifier = modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        // ---- Header ----
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically
            ) {
                IconButton(onClick = onBack) {
                    Icon(Icons.Default.ArrowBack, contentDescription = "返回数据页面")
                }
                Text(
                    "今日健康详情",
                    style = MaterialTheme.typography.titleLarge,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier.weight(1f)
                )
                IconButton(onClick = { viewModel.refreshHealth() }) {
                    Icon(Icons.Default.Refresh, contentDescription = "刷新健康数据")
                }
            }
        }

        // ---- Sleep Reference ----
        item {
            Text("睡眠参考", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        }

        item {
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(modifier = Modifier.padding(16.dp)) {
                    // Big sleep interval
                    Text(
                        health.sleepHeadline(),
                        style = MaterialTheme.typography.headlineSmall,
                        fontWeight = FontWeight.Bold
                    )
                    if (health?.sleep?.status == "final") {
                        Spacer(Modifier.height(4.dp))
                        Text(
                            "区间跨度 ${health.sleep.intervalSeconds.durationLabel()} · 实际无交互 ${health.sleep.restSeconds.durationLabel()}",
                            color = MaterialTheme.colorScheme.primary,
                            fontWeight = FontWeight.Medium
                        )
                    }

                    Spacer(Modifier.height(12.dp))
                    Divider()
                    Spacer(Modifier.height(8.dp))

                    InfoRow(
                        "睡前最后活跃",
                        health?.sleep?.lastActivityDevices?.deviceNames().orEmpty().ifBlank { "无数据" }
                    )
                    Spacer(Modifier.height(4.dp))
                    InfoRow(
                        "醒后最早使用",
                        health?.sleep?.firstActivityDevices?.deviceNames().orEmpty().ifBlank { "暂无" }
                    )

                    if (health?.sleep?.status == "final") {
                        Spacer(Modifier.height(4.dp))
                        InfoRow("短暂中断", health.sleep.interruptionSeconds.durationLabel())
                        Spacer(Modifier.height(4.dp))
                        InfoRow("完成时刻", health.sleep.finalizedAt.timeLabel())
                    } else {
                        Spacer(Modifier.height(4.dp))
                        Text(health.sleepStatusLabel(), style = MaterialTheme.typography.bodySmall)
                    }

                    Spacer(Modifier.height(8.dp))
                    Text(
                        if (uiState.healthInfoOffline) "当前离线中（显示最近一次中央结果）" else "估算值，仅供参考",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.4f)
                    )
                }
            }
        }

        // ---- Central step results ----
        item {
            Text("当日步数（单台设备）", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        }

        if (health?.steps?.devices?.isNotEmpty() == true) item {
            val devices = health.steps.devices
            val defaultDevice = CentralStepDeviceSelector.defaultDevice(devices)
            var selectedDeviceId by rememberSaveable(health.date) { mutableStateOf<String?>(null) }
            var deviceMenuExpanded by remember { mutableStateOf(false) }
            val selected = devices.firstOrNull { it.deviceId == selectedDeviceId } ?: defaultDevice
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                        Text("设备", style = MaterialTheme.typography.labelMedium)
                        Box {
                            TextButton(onClick = { deviceMenuExpanded = true }) { Text(selected?.displayName ?: "选择设备") }
                            DropdownMenu(expanded = deviceMenuExpanded, onDismissRequest = { deviceMenuExpanded = false }) {
                                devices.forEach { device -> DropdownMenuItem(
                                    text = { Text(device.displayName) },
                                    onClick = { selectedDeviceId = device.deviceId; deviceMenuExpanded = false }
                                ) }
                            }
                        }
                    }
                    Text(if (selected?.status == "available") "${selected.steps ?: 0} 步" else "样本不足，暂无法计算",
                        style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
                    Text("${selected?.sampleCount ?: 0} 条样本 · ${selected?.firstSampleAt.timeLabel()} 至 ${selected?.lastSampleAt.timeLabel()}", style = MaterialTheme.typography.bodySmall)
                    selected?.hourlySteps?.let { HourlyStepsChart(it) } ?: Text(
                        "中央尚未提供该设备的小时步数明细，无法绘制图表。",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.55f),
                        modifier = Modifier.padding(top = 12.dp)
                    )
                }
            }
        } else item {
            Card(modifier = Modifier.fillMaxWidth()) { Text("暂无中央步数结果", modifier = Modifier.padding(16.dp)) }
        }

        item {
            Text("本机活动参考", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        }
        item {
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(modifier = Modifier.padding(16.dp)) {
                    if (uiState.localActivityIntervals.isEmpty()) {
                        Text("暂无足够本机位置或计步证据", style = MaterialTheme.typography.bodySmall)
                    } else {
                        uiState.localActivityIntervals.forEach { interval ->
                            InfoRow(interval.label, "${Instant.ofEpochMilli(interval.startedAt).atZone(healthZone).format(healthTimeFormatter)} - ${Instant.ofEpochMilli(interval.endedAt).atZone(healthZone).format(healthTimeFormatter)}")
                            Spacer(Modifier.height(4.dp))
                        }
                    }
                    Text("仅根据本机 5 分钟观察推断，供活动参考，不代表医疗结论或真实交通方式。", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.5f))
                }
            }
        }

        // ---- Local diagnostics only; never presented as today’s steps. ----
        if (hasSensor && samples.isNotEmpty()) {
            item {
                Text("本机采集诊断：传感器累计值（非今日步数）",
                    style = MaterialTheme.typography.titleSmall,
                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f))
            }
            item { Text("已保存 ${samples.size} 条传感器观察；累计值仅用于采集诊断，不作为今日步数或活动结论。", style = MaterialTheme.typography.bodySmall) }
        } else if (hasSensor && samples.isEmpty()) {
            item {
                Card(modifier = Modifier.fillMaxWidth()) {
                    Text(
                        "传感器已就绪，等待后台采集。每日步数会由中央按相邻累计值计算。",
                        modifier = Modifier.padding(16.dp),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.5f)
                    )
                }
            }
        } else {
            item {
                Card(modifier = Modifier.fillMaxWidth()) {
                    Text(
                        "此设备不支持计步传感器",
                        modifier = Modifier.padding(16.dp),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.4f)
                    )
                }
            }
        }
    }
}

@Composable
private fun HourlyStepsChart(hourlySteps: List<Long>) {
    val max = hourlySteps.maxOrNull()?.coerceAtLeast(1L) ?: 1L
    Column(modifier = Modifier.fillMaxWidth().padding(top = 12.dp)) {
        Text("0–23 时步数", style = MaterialTheme.typography.labelMedium)
        Row(modifier = Modifier.fillMaxWidth().height(120.dp), horizontalArrangement = Arrangement.spacedBy(2.dp), verticalAlignment = Alignment.Bottom) {
            hourlySteps.forEach { steps ->
                val height = if (steps == 0L) 0f else (steps.toFloat() / max * 96f).coerceAtLeast(2f)
                Box(
                    modifier = Modifier.weight(1f).height(height.dp)
                        .background(Color(0xFF2EAF5D), RoundedCornerShape(topStart = 2.dp, topEnd = 2.dp))
                )
            }
        }
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text("0 时", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.5f))
            Text("23 时", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.5f))
        }
    }
}

@Composable
private fun InfoRow(label: String, value: String) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween
    ) {
        Text(label, style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f))
        Text(value, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.Medium)
    }
}

@Composable
private fun PhoneUsageCard(uiState: UiState) {
    val summary = uiState.todayUsageSummary
    val totalSeconds = summary.apps.sumOf { it.durationSeconds }
    val topApp = summary.apps.firstOrNull()
    val unsynced = (uiState.todayCollected - uiState.todaySynced).coerceAtLeast(0)

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(painterResource(R.drawable.ic_lucide_chart_bar), null,
                    tint = MaterialTheme.colorScheme.primary, modifier = Modifier.size(22.dp))
                Spacer(Modifier.width(8.dp))
                Text("应用使用", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
            }
            Spacer(modifier = Modifier.height(12.dp))
            Text(if (totalSeconds > 0) formatDuration(totalSeconds) else "暂无记录",
                style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
            Text(
                topApp?.let { "使用最多：${it.appName} · ${formatDuration(it.durationSeconds)}" }
                    ?: "今天还没有采集到应用使用记录",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
            if (summary.eventCount > 0) {
                Spacer(Modifier.height(6.dp))
                Text("记录时段 ${summary.timeRange} · ${summary.apps.size} 个应用",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            if (unsynced > 0) {
                Spacer(Modifier.height(8.dp))
                Text("$unsynced 条记录等待同步", style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSecondaryContainer,
                    modifier = Modifier.background(MaterialTheme.colorScheme.secondaryContainer, RoundedCornerShape(10.dp))
                        .padding(horizontal = 8.dp, vertical = 4.dp))
            }
        }
    }
}

@Composable
private fun DataTab(uiState: UiState, viewModel: MainViewModel, modifier: Modifier = Modifier) {
    var showLocalUsageDetails by rememberSaveable { mutableStateOf(false) }
    var showLocationDetails by rememberSaveable { mutableStateOf(false) }
    var showHealthDetails by rememberSaveable { mutableStateOf(false) }
    if (showLocationDetails) {
        LocationDetailsScreen(
            uiState = uiState,
            viewModel = viewModel,
            onBack = { showLocationDetails = false },
            modifier = modifier
        )
        return
    }
    if (showLocalUsageDetails) {
        LocalUsageDetailsScreen(
            uiState = uiState,
            viewModel = viewModel,
            onBack = { showLocalUsageDetails = false },
            modifier = modifier
        )
        return
    }
    if (showHealthDetails) {
        HealthDetailsScreen(
            uiState = uiState,
            viewModel = viewModel,
            onBack = { showHealthDetails = false },
            modifier = modifier
        )
        return
    }

    LaunchedEffect(Unit) {
        viewModel.loadTodayUsageSummary()
        viewModel.refreshLocationStatus()
        viewModel.refreshHealth()
    }

    LazyColumn(
        modifier = modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        item {
            Text("数据", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Text("查看手机本机采集与中央派生结果",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        item {
            LocationStatusCard(
                uiState = uiState,
                onRefresh = { viewModel.refreshLocationStatus() }
            )
        }

        item {
            OutlinedButton(
                onClick = { showLocationDetails = true },
                modifier = Modifier.fillMaxWidth()
            ) {
                Icon(Icons.Default.LocationOn, contentDescription = null)
                Spacer(modifier = Modifier.width(8.dp))
                Text("查看位置采集详情")
            }
        }

        // 手机使用信息
        item {
            PhoneUsageCard(uiState)
        }

        item {
            OutlinedButton(
                onClick = { showLocalUsageDetails = true },
                modifier = Modifier.fillMaxWidth()
            ) {
                Icon(Icons.Default.Analytics, contentDescription = null)
                Spacer(modifier = Modifier.width(8.dp))
                Text("查看应用使用详情")
            }
        }

        // 健康信息
        item {
            HealthInfoCard(uiState, viewModel)
        }

        item {
            OutlinedButton(
                onClick = { showHealthDetails = true },
                modifier = Modifier.fillMaxWidth()
            ) {
                Icon(Icons.Default.Bedtime, contentDescription = null)
                Spacer(modifier = Modifier.width(8.dp))
                Text("查看健康详情")
            }
        }
    }
}

@Composable
private fun LocationStatusCard(
    uiState: UiState,
    onRefresh: () -> Unit
) {
    val status = when {
        !uiState.locationPermissionGranted -> "等待位置权限"
        !uiState.locationTrackingEnabled -> "已关闭"
        uiState.locationServiceRunning -> "采集中"
        else -> "正在启动"
    }
    val statusColor = when (status) {
        "采集中" -> Success
        "正在启动" -> Warning
        else -> MaterialTheme.colorScheme.onSurfaceVariant
    }
    val locationText = uiState.lastLocation?.place?.displayLabel
        ?: if (uiState.lastLocation != null) "已获取定位" else "暂无有效定位"
    val lastDetectedText = uiState.lastLocationDetectedAt?.let { timestamp ->
        java.text.SimpleDateFormat("HH:mm", java.util.Locale.getDefault()).format(java.util.Date(timestamp))
    } ?: "尚未检测到位置"

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surface
        )
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.LocationOn, contentDescription = null, tint = statusColor)
                Spacer(modifier = Modifier.width(8.dp))
                Text("位置采集", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                Spacer(modifier = Modifier.weight(1f))
                Text(status, color = statusColor, fontWeight = FontWeight.Medium)
                IconButton(onClick = onRefresh) {
                    Icon(Icons.Default.Refresh, contentDescription = "刷新定位状态")
                }
            }
            Spacer(modifier = Modifier.height(12.dp))
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Column {
                    Text("今日已收集", style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Text(
                        "${uiState.todayLocationSummary.eventCount} 条记录",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold)
                }
                Column(horizontalAlignment = Alignment.End) {
                    Text("今日数据时段", style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Text(uiState.todayLocationSummary.timeRange, style = MaterialTheme.typography.bodyMedium,
                        fontWeight = FontWeight.Medium)
                }
            }
            if (uiState.todayLocationSummary.activeCount > 0) {
                Spacer(modifier = Modifier.height(6.dp))
                Text(
                    "${uiState.todayLocationSummary.activeCount} 个位置收集中",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.primary
                )
            }
            Spacer(modifier = Modifier.height(12.dp))
            Text("最新位置：$locationText", style = MaterialTheme.typography.bodySmall)
            Text("最近更新：$lastDetectedText", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            uiState.lastLocation?.place?.let { place ->
                Spacer(modifier = Modifier.height(6.dp))
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(place.displayLabel ?: "地址解析中", style = MaterialTheme.typography.bodySmall)
                    Text(place.precision, style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                place.fullAddress?.let { address ->
                    Text(address, style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            if (uiState.locationTrackingEnabled && uiState.todayLocationSummary.eventCount == 0) {
                Spacer(modifier = Modifier.height(6.dp))
                Text("位置采集已开启，正在等待新的有效定位。", style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable
private fun LocationDetailsScreen(
    uiState: UiState,
    viewModel: MainViewModel,
    onBack: () -> Unit,
    modifier: Modifier = Modifier
) {
    BackHandler(onBack = onBack)
    LaunchedEffect(Unit) { viewModel.loadTodayLocationDetails() }
    val details = uiState.todayLocationDetails

    LazyColumn(
        modifier = modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically
            ) {
                IconButton(onClick = onBack) {
                    Icon(Icons.Default.ArrowBack, contentDescription = "返回数据页面")
                }
                Text(
                    "今日位置采集详情",
                    style = MaterialTheme.typography.titleLarge,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier.weight(1f)
                )
                IconButton(onClick = { viewModel.loadTodayLocationDetails() }) {
                    Icon(Icons.Default.Refresh, contentDescription = "刷新位置详情")
                }
            }
        }

        item {
            Card(modifier = Modifier.fillMaxWidth()) {
                Row(
                    modifier = Modifier.padding(16.dp).fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column {
                        Text("独立同步位置", style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f))
                        Text("${details.syncEventCount} 个", style = MaterialTheme.typography.headlineMedium,
                            fontWeight = FontWeight.Bold)
                    }
                    Column(horizontalAlignment = Alignment.End) {
                        Text("有效定位数据", style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f))
                        Text("${details.samples.size} 条", style = MaterialTheme.typography.headlineMedium,
                            fontWeight = FontWeight.Bold)
                    }
                }
            }
        }

        item {
            Text("旧版位置段", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        }

        if (uiState.isLoadingTodayLocation) {
            item {
                Box(modifier = Modifier.fillMaxWidth().padding(32.dp), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                }
            }
        } else if (details.segments.isEmpty()) {
            item {
                Card(modifier = Modifier.fillMaxWidth()) {
                    Text(
                        "没有旧版位置段。当前版本不再在手机上聚类，新位置均在下方逐条展示。",
                        modifier = Modifier.padding(16.dp),
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f)
                    )
                }
            }
        } else {
            items(details.segments, key = { segment -> segment.id }) { segment ->
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                            Text(
                                if (segment.isActive) "收集中" else if (segment.kind == "stay") "停留" else "位置",
                                fontWeight = FontWeight.Medium,
                                color = if (segment.isActive) MaterialTheme.colorScheme.primary
                                else MaterialTheme.colorScheme.onSurface
                            )
                            Text(
                                formatDuration(segment.durationSeconds.toLong()),
                                fontWeight = FontWeight.Bold,
                                color = MaterialTheme.colorScheme.primary
                            )
                        }
                        Spacer(modifier = Modifier.height(6.dp))
                        Text(
                            "${formatLocationTime(segment.startedAt)} - ${formatLocationTime(segment.observedUntil)}",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.65f)
                        )
                        segment.placeLabel?.let { label ->
                            Spacer(modifier = Modifier.height(6.dp))
                            Text(label, style = MaterialTheme.typography.bodyMedium)
                        }
                        Spacer(modifier = Modifier.height(4.dp))
                        Text(
                            String.format(
                                java.util.Locale.getDefault(),
                                "%.5f, %.5f（±%.0fm）",
                                segment.latitude,
                                segment.longitude,
                                segment.accuracyMeters
                            ),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.65f)
                        )
                    }
                }
            }
        }

        item {
            Text("逐条位置数据（均独立同步）", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        }

        if (!uiState.isLoadingTodayLocation && details.samples.isEmpty()) {
            item {
                Card(modifier = Modifier.fillMaxWidth()) {
                    Text(
                        "今天还没有有效定位数据。",
                        modifier = Modifier.padding(16.dp),
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f)
                    )
                }
            }
        } else if (!uiState.isLoadingTodayLocation) {
            itemsIndexed(
                details.samples,
                key = { index, sample -> "${sample.observedAt}-$index" }
            ) { index, sample ->
                val previousSample = details.samples.getOrNull(index - 1)
                val intervalText = previousSample?.let { previous ->
                    "与上一条间隔：${formatLocationInterval(sample.observedAt - previous.observedAt)}"
                } ?: "当天首条有效定位"
                val motionText = when {
                    sample.motionWindowStartedAt <= 0L -> "加速度：旧数据未记录"
                    !sample.accelerometerAvailable -> "加速度：传感器不可用"
                    sample.motionTriggerCount > 0 -> String.format(
                        java.util.Locale.getDefault(),
                        "加速度：触发 %d 次 / %d 次采样 · 峰值 %.2f m/s²（阈值 %.2f）",
                        sample.motionTriggerCount,
                        sample.accelerometerSampleCount,
                        sample.peakMotionDeltaMetersPerSecondSquared,
                        sample.motionThresholdMetersPerSecondSquared
                    )
                    else -> String.format(
                        java.util.Locale.getDefault(),
                        "加速度：未触发 / %d 次采样 · 峰值 %.2f m/s²（阈值 %.2f）",
                        sample.accelerometerSampleCount,
                        sample.peakMotionDeltaMetersPerSecondSquared,
                        sample.motionThresholdMetersPerSecondSquared
                    )
                }
                Card(modifier = Modifier.fillMaxWidth()) {
                    Row(
                        modifier = Modifier.padding(16.dp).fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text(formatLocationTime(sample.observedAt), fontWeight = FontWeight.Medium)
                            Text(
                                intervalText,
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.primary
                            )
                            Text(
                                String.format(
                                    java.util.Locale.getDefault(),
                                    "%.5f, %.5f（±%.0fm）",
                                    sample.latitude,
                                    sample.longitude,
                                    sample.accuracyMeters
                                ),
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.65f)
                            )
                            Text(
                                motionText,
                                style = MaterialTheme.typography.bodySmall,
                                color = if (sample.motionTriggerCount > 0) MaterialTheme.colorScheme.primary
                                else MaterialTheme.colorScheme.onSurface.copy(alpha = 0.65f)
                            )
                            if (sample.motionWindowStartedAt > 0L) {
                                Text(
                                    "统计窗口：${formatLocationTime(sample.motionWindowStartedAt)} - ${formatLocationTime(sample.observedAt)}",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.55f)
                                )
                            }
                        }
                        Text(
                            sample.provider,
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.55f)
                        )
                    }
                }
            }
        }
    }
}

private fun formatLocationTime(timestamp: Long): String =
    java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.getDefault()).format(java.util.Date(timestamp))

private fun formatLocationInterval(intervalMillis: Long): String {
    val totalSeconds = (intervalMillis.coerceAtLeast(0L) / 1000L)
    val hours = totalSeconds / 3600L
    val minutes = (totalSeconds % 3600L) / 60L
    val seconds = totalSeconds % 60L
    return when {
        hours > 0L -> "${hours}小时${minutes}分${seconds}秒"
        minutes > 0L -> "${minutes}分${seconds}秒"
        else -> "${seconds}秒"
    }
}

@Composable
private fun LocalUsageDetailsScreen(
    uiState: UiState,
    viewModel: MainViewModel,
    onBack: () -> Unit,
    modifier: Modifier = Modifier
) {
    BackHandler(onBack = onBack)
    LaunchedEffect(Unit) { viewModel.loadTodayUsageSummary() }
    val summary = uiState.todayUsageSummary
    val longestDuration = summary.apps.maxOfOrNull { it.durationSeconds } ?: 0L

    LazyColumn(
        modifier = modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically
            ) {
                IconButton(onClick = onBack) {
                    Icon(Icons.Default.ArrowBack, contentDescription = "返回数据页面")
                }
                Text(
                    "今日本机采集详情",
                    style = MaterialTheme.typography.titleLarge,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier.weight(1f)
                )
                IconButton(onClick = { viewModel.loadTodayUsageSummary() }) {
                    Icon(Icons.Default.Refresh, contentDescription = "刷新")
                }
            }
        }

        item {
            Card(modifier = Modifier.fillMaxWidth()) {
                Row(
                    modifier = Modifier.padding(16.dp).fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column {
                        Text("已收集事件", style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f))
                        Text("${summary.eventCount} 条", style = MaterialTheme.typography.headlineMedium,
                            fontWeight = FontWeight.Bold)
                    }
                    Column(horizontalAlignment = Alignment.End) {
                        Text("今日记录时段", style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f))
                        Text(summary.timeRange, style = MaterialTheme.typography.titleMedium,
                            fontWeight = FontWeight.Medium)
                    }
                }
            }
        }

        item {
            Text("各应用使用时长", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        }

        if (uiState.isLoadingTodayUsage) {
            item {
                Box(modifier = Modifier.fillMaxWidth().padding(32.dp), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                }
            }
        } else if (summary.apps.isEmpty()) {
            item {
                Card(modifier = Modifier.fillMaxWidth()) {
                    Text(
                        "今天还没有已完成的应用使用记录。授权后切换几个应用，等待下一次采集即可。",
                        modifier = Modifier.padding(16.dp),
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f)
                    )
                }
            }
        } else {
            items(summary.apps) { app ->
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                            Column(modifier = Modifier.weight(1f)) {
                                Text(app.appName, fontWeight = FontWeight.Medium)
                                Text(
                                    "${app.eventCount} 条记录",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f)
                                )
                            }
                            Text(formatDuration(app.durationSeconds), fontWeight = FontWeight.Bold,
                                color = MaterialTheme.colorScheme.primary)
                        }
                        Spacer(modifier = Modifier.height(10.dp))
                        LinearProgressIndicator(
                            progress = if (longestDuration == 0L) 0f else app.durationSeconds.toFloat() / longestDuration,
                            modifier = Modifier.fillMaxWidth(),
                            color = MaterialTheme.colorScheme.primary,
                            trackColor = MaterialTheme.colorScheme.primary.copy(alpha = 0.12f)
                        )
                    }
                }
            }
        }
    }
}

private fun formatDuration(seconds: Long): String {
    val h = seconds / 3600
    val m = (seconds % 3600) / 60
    val s = seconds % 60
    return when {
        h > 0 -> "${h}h ${m}m"
        m > 0 -> "${m}m ${s}s"
        else -> "${s}s"
    }
}

// ==================== 设置页 ====================

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SettingsTab(uiState: UiState, viewModel: MainViewModel, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    var invitationText by rememberSaveable { mutableStateOf("") }
    var showRebind by rememberSaveable { mutableStateOf(!uiState.centralTokenConfigured) }
    var showAdvanced by rememberSaveable { mutableStateOf(false) }
    val locationPermissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { permissions ->
        val granted = permissions[android.Manifest.permission.ACCESS_FINE_LOCATION] == true ||
            permissions[android.Manifest.permission.ACCESS_COARSE_LOCATION] == true
        if (granted) viewModel.setLocationTrackingEnabled(true) else viewModel.refreshLocationStatus()
    }
    LazyColumn(
        modifier = modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        item {
            Text("设置", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Text(
                "集中管理手机采集、中央连接与同步频率",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }

        item {
            DataSyncCard(uiState = uiState, onSync = { viewModel.triggerSync() })
        }

        item {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Text("业务日起点", style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.Bold)
                    Text(
                        "${uiState.sharedDayStartHour.toString().padStart(2, '0')}:00",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold
                    )
                    Text(
                        if (uiState.sharedSettingsLoadedFromCentral) "由中央统一设置，手机端只读"
                        else "等待从中央服务获取统一设置",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        }

        item {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Text("采集与权限", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                    Spacer(modifier = Modifier.height(8.dp))
                    Text("应用使用采集", style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.Medium)
                    Text(
                        if (uiState.usageStatsPermissionGranted) "已允许读取应用使用情况，用于生成每日使用记录。"
                        else "需要在系统设置中允许 Life Link 访问应用使用情况。",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(onClick = {
                            context.startActivity(Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS))
                        }) {
                            Text(if (uiState.usageStatsPermissionGranted) "打开系统设置" else "授予使用情况访问")
                        }
                        OutlinedButton(onClick = { viewModel.refreshNativeCollectionStatus() }) {
                            Text("重新检查")
                        }
                    }
                    Spacer(modifier = Modifier.height(16.dp))
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text("位置采集", style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.Medium)
                            Text(
                                if (uiState.locationPermissionGranted) {
                                    "已获得位置权限。采集频率会根据活动状态自动调整。"
                                } else {
                                    "开启后需要授予位置权限。"
                                },
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                        Switch(
                            checked = uiState.locationTrackingEnabled,
                            onCheckedChange = { enabled ->
                                if (enabled && !uiState.locationPermissionGranted) {
                                    locationPermissionLauncher.launch(
                                        arrayOf(
                                            android.Manifest.permission.ACCESS_FINE_LOCATION,
                                            android.Manifest.permission.ACCESS_COARSE_LOCATION
                                        )
                                    )
                                } else {
                                    viewModel.setLocationTrackingEnabled(enabled)
                                }
                            }
                        )
                    }
                }
            }
        }

        item {
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
                ) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("中央服务", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                            Spacer(modifier = Modifier.weight(1f))
                            Surface(
                                color = if (uiState.centralTokenConfigured) MaterialTheme.colorScheme.primaryContainer
                                else MaterialTheme.colorScheme.surfaceVariant,
                                shape = RoundedCornerShape(8.dp)
                            ) {
                                Text(
                                    if (uiState.centralTokenConfigured) "已绑定" else "未绑定",
                                    modifier = Modifier.padding(horizontal = 8.dp, vertical = 3.dp),
                                    style = MaterialTheme.typography.labelSmall,
                                    color = if (uiState.centralTokenConfigured) MaterialTheme.colorScheme.onPrimaryContainer
                                    else MaterialTheme.colorScheme.onSurfaceVariant
                                )
                            }
                        }
                        Spacer(modifier = Modifier.height(6.dp))
                        Text(
                            "手机仅向已绑定的中央服务上传本机数据。",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                        if (uiState.centralBaseUrl.isNotBlank()) {
                            Text(uiState.centralBaseUrl, style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        Spacer(modifier = Modifier.height(8.dp))
                        if (uiState.centralTokenConfigured && !showRebind) {
                            OutlinedButton(onClick = { showRebind = true }) { Text("重新绑定") }
                        } else {
                            OutlinedTextField(
                                value = invitationText,
                                onValueChange = { invitationText = it },
                                label = { Text("设备配对码（LR1）") },
                                modifier = Modifier.fillMaxWidth(),
                                singleLine = true,
                                visualTransformation = PasswordVisualTransformation()
                            )
                            Spacer(modifier = Modifier.height(8.dp))
                            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                Button(onClick = {
                                    viewModel.previewCentralInvitation(invitationText)
                                    invitationText = ""
                                }, enabled = invitationText.trim().startsWith("LR1.")) {
                                    Text("检查配对码")
                                }
                                if (uiState.centralTokenConfigured) {
                                    OutlinedButton(onClick = {
                                        showRebind = false
                                        invitationText = ""
                                    }) { Text("取消") }
                                }
                            }
                        }
                        uiState.invitationPreview?.let { preview ->
                            Spacer(modifier = Modifier.height(12.dp))
                            Text("中央地址：${preview.centralBaseUrl}", style = MaterialTheme.typography.bodySmall)
                            Text("权限：${preview.permissionLabel}", style = MaterialTheme.typography.bodySmall)
                            Text("有效期至：${preview.expiresAt}", style = MaterialTheme.typography.bodySmall)
                            Text("本机名：${preview.deviceName}", style = MaterialTheme.typography.bodySmall)
                            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                Button(onClick = { viewModel.confirmCentralInvitation() }, enabled = !uiState.enrollmentInProgress) {
                                    Text(if (uiState.enrollmentInProgress) "正在绑定…" else "确认绑定")
                                }
                                OutlinedButton(onClick = { viewModel.cancelCentralInvitation() }, enabled = !uiState.enrollmentInProgress) {
                                    Text("取消")
                                }
                            }
                        }
                        if (uiState.enrollmentMessage.isNotBlank()) Text(uiState.enrollmentMessage, style = MaterialTheme.typography.bodySmall)
                        if (uiState.centralLastStatus.isNotBlank()) {
                            Spacer(modifier = Modifier.height(8.dp))
                            Text(
                                uiState.centralLastStatus,
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                        TextButton(onClick = { showAdvanced = !showAdvanced }) {
                            Text(if (showAdvanced) "收起高级信息" else "高级信息")
                        }
                        if (showAdvanced) {
                            Text("设备 ID", style = MaterialTheme.typography.labelMedium)
                            Text(uiState.centralDeviceId, style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                }
        }

        item {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Text("自动同步", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                    Text(
                        "选择手机后台上传数据的频率",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    listOf(listOf(5, 10, 15), listOf(30, 60)).forEach { rowOptions ->
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            rowOptions.forEach { minutes ->
                                FilterChip(
                                    selected = uiState.syncIntervalMinutes == minutes,
                                    onClick = { viewModel.updateSyncInterval(minutes) },
                                    label = { Text("$minutes 分钟") }
                                )
                            }
                        }
                    }
                }
            }
        }

    }
}

@Composable
private fun StatusItem(label: String, value: String) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Text(value, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        Text(label, style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f))
    }
}
