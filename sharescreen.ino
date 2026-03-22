#include <TFT_eSPI.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <TJpg_Decoder.h>
#include <esp_task_wdt.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <esp_system.h>
#include <esp_heap_caps.h>

// ============================
// 稳定性配置优化
// ============================

// SPI配置优化
#define SPI_FREQUENCY       27000000  // 降低到27MHz提高稳定性
#define SPI_READ_FREQUENCY  20000000
#define SPI_TOUCH_FREQUENCY 2500000

// 内存配置优化
#define JPEG_BUFFER_SIZE    20000     // 减小缓冲区大小
#define MIN_FREE_HEAP       20000     // 降低内存阈值
#define MIN_FREE_PSRAM      50000     // PSRAM阈值

// 看门狗配置
#define WDT_TIMEOUT         15        // 15秒看门狗
#define TASK_STACK_SIZE     4096      // 任务栈大小

// 网络配置优化
#define HTTP_TIMEOUT        8000      // 8秒超时
#define WIFI_CONNECT_TIMEOUT 20000    // 20秒连接超时
#define MAX_RETRY_COUNT     5         // 最大重试次数

// 性能优化
#define FPS_UPDATE_INTERVAL 3000      // 3秒更新FPS
#define SYSTEM_STATUS_INTERVAL 30000  // 30秒状态输出
#define MIN_FRAME_SIZE      500       // 最小帧大小
#define MAX_FRAME_SIZE      18000     // 最大帧大小

// TFT对象
#include "SPI.h"
#include <TFT_eSPI.h>
TFT_eSPI tft = TFT_eSPI();

// DMA缓冲区
#ifdef USE_DMA
  uint16_t dmaBuffer1[256];  // 增大到256*2字节
  uint16_t dmaBuffer2[256];
  uint16_t* dmaBufferPtr = dmaBuffer1;
  volatile bool dmaBufferSel = false;
  volatile bool dmaInProgress = false;
#endif

// WiFi配置
const char* ssid = "HUAWEI P50 Pro";
const char* password = "12345678";
const char* serverURL = "http://192.168.43.220:5000/jpeg_frame";

// 双缓冲管理
uint8_t* jpegBuffer1 = NULL;
uint8_t* jpegBuffer2 = NULL;
volatile uint8_t* currentBuffer = NULL;
volatile uint8_t* processingBuffer = NULL;
volatile bool bufferReady = false;
portMUX_TYPE bufferMux = portMUX_INITIALIZER_UNLOCKED;

// 统计信息
struct SystemStats {
  unsigned long lastFpsTime;
  int frameCount;
  int decodeErrors;
  int networkErrors;
  unsigned long lastStatusTime;
  unsigned long lastSuccessTime;
  unsigned long lastHeapCheck;
  int errorCount;
  int consecutiveErrors;
  int totalFrames;
  float avgDecodeTime;
  unsigned long lastDecodeStart;
} stats = {0};

// 任务句柄
TaskHandle_t wifiTaskHandle = NULL;
TaskHandle_t displayTaskHandle = NULL;

// 同步信号
SemaphoreHandle_t jpegSemaphore;
QueueHandle_t jpegQueue;

// ===========================================
// 内存管理优化
// ===========================================
bool initBuffers() {
  // 优先使用PSRAM，失败则使用内部RAM
  if (psramFound() && ESP.getPsramSize() > 1024 * 1024) {
    Serial.println("PSRAM detected, allocating buffers in PSRAM");
    jpegBuffer1 = (uint8_t*)ps_malloc(JPEG_BUFFER_SIZE);
    jpegBuffer2 = (uint8_t*)ps_malloc(JPEG_BUFFER_SIZE);
  } else {
    Serial.println("Using internal RAM for buffers");
    jpegBuffer1 = (uint8_t*)heap_caps_malloc(JPEG_BUFFER_SIZE, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    jpegBuffer2 = (uint8_t*)heap_caps_malloc(JPEG_BUFFER_SIZE, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  }
  
  if (jpegBuffer1 == NULL || jpegBuffer2 == NULL) {
    Serial.println("ERROR: Failed to allocate buffers!");
    return false;
  }
  
  // 清零缓冲区
  memset(jpegBuffer1, 0, JPEG_BUFFER_SIZE);
  memset(jpegBuffer2, 0, JPEG_BUFFER_SIZE);
  
  Serial.printf("Buffers allocated: %d bytes each\n", JPEG_BUFFER_SIZE);
  return true;
}

void freeBuffers() {
  if (jpegBuffer1) {
    free(jpegBuffer1);
    jpegBuffer1 = NULL;
  }
  if (jpegBuffer2) {
    free(jpegBuffer2);
    jpegBuffer2 = NULL;
  }
}

// ===========================================
// JPEG 输出回调（简化版，禁用DMA）
// ===========================================
// This next function will be called during decoding of the jpeg file to render each
// 16x16 or 8x8 image tile (Minimum Coding Unit) to the TFT.
bool tft_output_dma(int16_t x, int16_t y, uint16_t w, uint16_t h, uint16_t* bitmap)
{
   // Stop further decoding as image is running off bottom of screen
  if ( y >= tft.height() ) return 0;

  // STM32F767 processor takes 43ms just to decode (and not draw) jpeg (-Os compile option)
  // Total time to decode and also draw to TFT:
  // SPI 54MHz=71ms, with DMA 50ms, 71-43 = 28ms spent drawing, so DMA is complete before next MCU block is ready
  // Apparent performance benefit of DMA = 71/50 = 42%, 50 - 43 = 7ms lost elsewhere
  // SPI 27MHz=95ms, with DMA 52ms. 95-43 = 52ms spent drawing, so DMA is *just* complete before next MCU block is ready!
  // Apparent performance benefit of DMA = 95/52 = 83%, 52 - 43 = 9ms lost elsewhere
#ifdef USE_DMA
  // Double buffering is used, the bitmap is copied to the buffer by pushImageDMA() the
  // bitmap can then be updated by the jpeg decoder while DMA is in progress
  if (dmaBufferSel) dmaBufferPtr = dmaBuffer2;
  else dmaBufferPtr = dmaBuffer1;
  dmaBufferSel = !dmaBufferSel; // Toggle buffer selection
  //  pushImageDMA() will clip the image block at screen boundaries before initiating DMA
  tft.pushImageDMA(x, y, w, h, bitmap, dmaBufferPtr); // Initiate DMA - blocking only if last DMA is not complete
  // tft.dmaWait();  
  // The DMA transfer of image block to the TFT is now in progress...
#else
  // Non-DMA blocking alternative
  tft.pushImage(x, y, w, h, bitmap);  // Blocking, so only returns when image block is drawn
#endif
  // Return 1 to decode next block.
  return 1;
}


// ===========================================
// 系统监控优化
// ===========================================
void printSystemStatus() {
  unsigned long now = millis();
  
  if (now - stats.lastStatusTime > SYSTEM_STATUS_INTERVAL) {
    Serial.println("\n========== System Status ==========");
    Serial.printf("Uptime: %lu s\n", now / 1000);
    Serial.printf("Free Heap: %d bytes (%d KB)\n", ESP.getFreeHeap(), ESP.getFreeHeap() / 1024);
    Serial.printf("Min Free Heap: %d bytes\n", ESP.getMinFreeHeap());
    Serial.printf("Max Alloc Heap: %d bytes\n", ESP.getMaxAllocHeap());
    
    if (psramFound()) {
      Serial.printf("Free PSRAM: %d bytes (%d KB)\n", ESP.getFreePsram(), ESP.getFreePsram() / 1024);
      Serial.printf("Total PSRAM: %d bytes\n", ESP.getPsramSize());
    }
    
    Serial.printf("Total Frames: %d\n", stats.totalFrames);
    Serial.printf("Frame Rate: %.1f fps\n", stats.frameCount * 1000.0 / FPS_UPDATE_INTERVAL);
    Serial.printf("Decode Errors: %d\n", stats.decodeErrors);
    Serial.printf("Network Errors: %d\n", stats.networkErrors);
    Serial.printf("Current Error Rate: %d\n", stats.consecutiveErrors);
    Serial.printf("Avg Decode Time: %.1f ms\n", stats.avgDecodeTime);
    Serial.printf("Core Temperature: %.1f°C\n", temperatureRead());
    Serial.println("====================================\n");
    
    stats.lastStatusTime = now;
    stats.frameCount = 0;
  }
}

bool checkSystemHealth() {
  unsigned long now = millis();
  
  // 每10秒检查一次内存
  if (now - stats.lastHeapCheck > 10000) {
    int freeHeap = ESP.getFreeHeap();
    
    if (freeHeap < MIN_FREE_HEAP) {
      Serial.printf("WARNING: Low memory! Free heap: %d bytes\n", freeHeap);
      stats.errorCount++;
      
      if (freeHeap < 10000) {
        Serial.println("CRITICAL: Extremely low memory!");
        return false;
      }
    }
    
    // 检查PSRAM
    if (psramFound() && ESP.getFreePsram() < MIN_FREE_PSRAM) {
      Serial.printf("WARNING: Low PSRAM! Free: %d bytes\n", ESP.getFreePsram());
    }
    
    stats.lastHeapCheck = now;
  }
  
  // 检查连续错误
  if (stats.consecutiveErrors > MAX_RETRY_COUNT) {
    Serial.printf("CRITICAL: Too many consecutive errors: %d\n", stats.consecutiveErrors);
    return false;
  }
  
  // 检查长时间无成功帧
  if (stats.lastSuccessTime > 0 && (now - stats.lastSuccessTime) > 45000) {
    Serial.println("CRITICAL: No successful frames for 45 seconds");
    return false;
  }
  
  return true;
}

void safeRestart() {
  Serial.println("\n=== Performing safe restart ===");
  Serial.flush();
  
  // 清理资源
  freeBuffers();
  if (jpegSemaphore) vSemaphoreDelete(jpegSemaphore);
  if (jpegQueue) vQueueDelete(jpegQueue);
  
  delay(1000);
  esp_restart();
}

// ===========================================
// WiFi管理优化
// ===========================================
bool connectWiFi() {
  Serial.print("Connecting to WiFi");
  
  // 显示连接状态
  tft.fillRect(0, 0, tft.width(), 20, TFT_BLACK);
  tft.setCursor(0, 0);
  tft.setTextColor(TFT_YELLOW, TFT_BLACK);
  tft.print("WiFi Connecting");
  
  WiFi.disconnect(true);
  delay(100);
  
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(false);
  WiFi.setTxPower(WIFI_POWER_11dBm);  // 降低发射功率提高稳定性
  
  WiFi.begin(ssid, password);
  
  unsigned long startTime = millis();
  int dotCount = 0;
  
  while (WiFi.status() != WL_CONNECTED && (millis() - startTime) < WIFI_CONNECT_TIMEOUT) {
    delay(500);
    Serial.print(".");
    
    // 更新显示
    if (++dotCount % 2 == 0) {
      tft.print(".");
    }
    
    esp_task_wdt_reset();
  }
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi Connected!");
    Serial.printf("IP: %s\n", WiFi.localIP().toString().c_str());
    Serial.printf("RSSI: %d dBm\n", WiFi.RSSI());
    
    tft.fillRect(0, 0, tft.width(), 20, TFT_BLACK);
    tft.setCursor(0, 0);
    tft.setTextColor(TFT_GREEN, TFT_BLACK);
    tft.printf("WiFi OK IP:%s", WiFi.localIP().toString().substring(0, 12).c_str());
    
    stats.errorCount = 0;
    stats.consecutiveErrors = 0;
    return true;
  } else {
    Serial.println("\nWiFi connection failed!");
    tft.fillRect(0, 0, tft.width(), 20, TFT_BLACK);
    tft.setCursor(0, 0);
    tft.setTextColor(TFT_RED, TFT_BLACK);
    tft.print("WiFi Failed!");
    return false;
  }
}

// ===========================================
// JPEG解码优化
// ===========================================
bool displayJPEG(uint8_t* jpgData, size_t len) {
  // 验证JPEG格式
  if (len < 100 || jpgData[0] != 0xFF || jpgData[1] != 0xD8) {
    Serial.println("Invalid JPEG header");
    return false;
  }
  
  // 记录解码时间
  stats.lastDecodeStart = millis();
  
  bool success = false;
  
  // 开始SPI传输
  tft.startWrite();
  
  // 解码JPEG
  JRESULT result = TJpgDec.drawJpg(0, 0, jpgData, len);
  
  if (result == JDR_OK) {
    success = true;
    
    // 计算解码时间
    unsigned long decodeTime = millis() - stats.lastDecodeStart;
    stats.avgDecodeTime = stats.avgDecodeTime * 0.9 + decodeTime * 0.1;
    
    stats.totalFrames++;
    stats.frameCount++;
    stats.lastSuccessTime = millis();
    stats.consecutiveErrors = 0;
  } else {
    // Serial.printf("JPEG decode error: %d at position %d\n", result, TJpgDec.getLastErrorPos());
    stats.decodeErrors++;
    stats.consecutiveErrors++;
    
    // 显示错误信息（仅显示短暂时间）
    tft.fillRect(0, tft.height() - 20, 100, 20, TFT_RED);
    tft.setTextColor(TFT_WHITE, TFT_RED);
    tft.setCursor(2, tft.height() - 16);
    tft.printf("Decode Err:%d", result);
  }
  
  tft.endWrite();
  
  return success;
}

// ===========================================
// FPS显示优化
// ===========================================
void updateDisplayInfo() {
  static unsigned long lastUpdate = 0;
  unsigned long now = millis();
  
  if (now - lastUpdate > FPS_UPDATE_INTERVAL) {
    float fps = stats.frameCount * 1000.0 / (now - stats.lastFpsTime);
    
    // 更新FPS显示
    tft.setTextColor(TFT_CYAN, TFT_BLACK);
    tft.setTextSize(1);
    tft.fillRect(0, tft.height() - 16, 80, 16, TFT_BLACK);
    tft.setCursor(2, tft.height() - 12);
    tft.printf("FPS:%.1f", fps);
    
    // 显示内存状态
    tft.fillRect(80, tft.height() - 16, 60, 16, TFT_BLACK);
    tft.setCursor(82, tft.height() - 12);
    tft.printf("M:%dK", ESP.getFreeHeap() / 1024);
    
    // 显示WiFi信号强度
    if (WiFi.status() == WL_CONNECTED) {
      int rssi = WiFi.RSSI();
      tft.fillRect(140, tft.height() - 16, 50, 16, TFT_BLACK);
      tft.setCursor(142, tft.height() - 12);
      tft.printf("RSSI:%d", rssi);
    }
    
    stats.lastFpsTime = now;
    stats.frameCount = 0;
    lastUpdate = now;
  }
}

// ===========================================
// WiFi任务
// ===========================================
void wifiTask(void* parameter) {
  while (1) {
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("WiFi disconnected, reconnecting...");
      connectWiFi();
      vTaskDelay(pdMS_TO_TICKS(5000));
    } else {
      vTaskDelay(pdMS_TO_TICKS(1000));
    }
    
    esp_task_wdt_reset();
  }
}

// ===========================================
// HTTP请求优化
// ===========================================
void fetchJPEGFrame() {
  if (WiFi.status() != WL_CONNECTED) {
    return;
  }
  
  HTTPClient http;
  
  // 获取下一个可用缓冲区
  uint8_t* buffer;
  portENTER_CRITICAL(&bufferMux);
  buffer = (currentBuffer == jpegBuffer1) ? jpegBuffer2 : jpegBuffer1;
  portEXIT_CRITICAL(&bufferMux);
  
  // HTTP配置优化
  http.setReuse(false);
  http.setTimeout(HTTP_TIMEOUT);
  http.setConnectTimeout(3000);
  
  if (!http.begin(serverURL)) {
    stats.networkErrors++;
    stats.consecutiveErrors++;
    return;
  }
  
  // 添加请求头
  http.addHeader("Connection", "close");
  http.addHeader("User-Agent", "ESP32-S3/1.0");
  
  int httpCode = http.GET();
  
  if (httpCode == HTTP_CODE_OK) {
    int len = http.getSize();
    
    // 验证帧大小
    if (len >= MIN_FRAME_SIZE && len <= MAX_FRAME_SIZE) {
      WiFiClient* stream = http.getStreamPtr();
      size_t bytesRead = 0;
      unsigned long startTime = millis();
      
      // 带超时的数据读取
      while (bytesRead < (size_t)len && (millis() - startTime) < HTTP_TIMEOUT) {
        if (stream->available()) {
          int chunk = stream->read(buffer + bytesRead, 
                                   min(512, (int)(len - bytesRead)));
          if (chunk > 0) {
            bytesRead += chunk;
          }
        } else {
          vTaskDelay(pdMS_TO_TICKS(5));
        }
        
        // 定期喂狗
        if (bytesRead % 4096 == 0) {
          esp_task_wdt_reset();
        }
      }
      
      if (bytesRead == (size_t)len) {
        // 验证JPEG数据
        if (buffer[0] == 0xFF && buffer[1] == 0xD8) {
          // 切换缓冲区
          portENTER_CRITICAL(&bufferMux);
          processingBuffer = currentBuffer;
          currentBuffer = buffer;
          bufferReady = true;
          portEXIT_CRITICAL(&bufferMux);
          
          // 发送解码信号
          xSemaphoreGive(jpegSemaphore);
          
          stats.networkErrors = 0;
        } else {
          stats.networkErrors++;
          stats.consecutiveErrors++;
        }
      } else {
        stats.networkErrors++;
        stats.consecutiveErrors++;
      }
    } else {
      stats.networkErrors++;
      stats.consecutiveErrors++;
    }
  } else {
    stats.networkErrors++;
    stats.consecutiveErrors++;
  }
  
  http.end();
}

// ===========================================
// 显示任务
// ===========================================
void displayTask(void* parameter) {
  while (1) {
    // 等待JPEG数据
    if (xSemaphoreTake(jpegSemaphore, pdMS_TO_TICKS(1000)) == pdTRUE) {
      uint8_t* buffer;
      
      portENTER_CRITICAL(&bufferMux);
      buffer = (uint8_t*)processingBuffer;
      bufferReady = false;
      portEXIT_CRITICAL(&bufferMux);
      
      if (buffer) {
        // 解码并显示
        displayJPEG(buffer, JPEG_BUFFER_SIZE);
        updateDisplayInfo();
      }
    }
    
    // 系统健康检查
    if (!checkSystemHealth()) {
      safeRestart();
    }
    
    // 打印状态
    printSystemStatus();
    
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

// ===========================================
// 初始化
// ===========================================
void setup() {
  Serial.begin(115200);
  delay(1000);
  
  Serial.println("\n\n=== ESP32-S3 Video Stream Display ===");
  Serial.printf("Chip Model: %s\n", ESP.getChipModel());
  Serial.printf("CPU Frequency: %d MHz\n", getCpuFrequencyMhz());
  
  // 分配内存
  if (!initBuffers()) {
    Serial.println("Buffer allocation failed!");
    delay(2000);
    safeRestart();
  }
  
  // 创建同步对象
  jpegSemaphore = xSemaphoreCreateBinary();
  jpegQueue = xQueueCreate(2, sizeof(uint8_t*));
  
  if (!jpegSemaphore || !jpegQueue) {
    Serial.println("Failed to create synchronization objects!");
    safeRestart();
  }
  
  // 配置看门狗
  esp_task_wdt_init(WDT_TIMEOUT, true);
  esp_task_wdt_add(NULL);
  
  // 初始化TFT
  tft.begin();
#ifdef USE_DMA
  tft.initDMA();
#endif
  tft.setRotation(1);
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextSize(1);
  
  // 显示启动信息
  tft.setCursor(0, 0);
  tft.println("ESP32-S3 Video Stream");
  tft.printf("CPU: %d MHz\n", getCpuFrequencyMhz());
  tft.printf("Heap: %d KB\n", ESP.getFreeHeap() / 1024);
  if (psramFound()) {
    tft.printf("PSRAM: %d KB\n", ESP.getPsramSize() / 1024);
  }
  
  // 配置JPEG解码器
  TJpgDec.setSwapBytes(true);
  TJpgDec.setJpgScale(1);
#ifdef USE_DMA
  TJpgDec.setCallback(tft_output_dma);
#else
  TJpgDec.setCallback(tft_output);
#endif
  
  // 连接WiFi
  delay(1000);
  if (!connectWiFi()) {
    tft.println("\nWiFi Failed! Retrying...");
    delay(3000);
    connectWiFi();
  }
  
  // 创建任务
  xTaskCreatePinnedToCore(
    wifiTask,
    "WiFiTask",
    TASK_STACK_SIZE,
    NULL,
    1,
    &wifiTaskHandle,
    0
  );
  
  xTaskCreatePinnedToCore(
    displayTask,
    "DisplayTask",
    TASK_STACK_SIZE * 2,
    NULL,
    2,
    &displayTaskHandle,
    1
  );
  
  stats.lastFpsTime = millis();
  stats.lastStatusTime = millis();
  stats.lastHeapCheck = millis();
  stats.lastSuccessTime = millis();
  
  Serial.println("System initialized successfully!");
  tft.fillScreen(TFT_BLACK);
  tft.setCursor(0, 0);
  tft.println("System Ready");
  tft.println("Streaming...");
  
  delay(2000);
}

// ===========================================
// 主循环优化
// ===========================================
void loop() {
  // 喂狗
  esp_task_wdt_reset();
  
  // 获取JPEG帧
  fetchJPEGFrame();
  
  // 动态延时控制帧率
  static unsigned long lastFetch = 0;
  unsigned long fetchInterval = 100;  // 基础间隔100ms
  
  // 根据错误率调整请求频率
  if (stats.consecutiveErrors > 2) {
    fetchInterval = 500;  // 出错时降低频率
  } else if (stats.decodeErrors > 5) {
    fetchInterval = 200;
  }
  
  unsigned long now = millis();
  unsigned long elapsed = now - lastFetch;
  
  if (elapsed < fetchInterval) {
    vTaskDelay(pdMS_TO_TICKS(fetchInterval - elapsed));
  }
  
  lastFetch = millis();
  
  // 强制垃圾回收（每100次循环）
  static int loopCount = 0;
  if (++loopCount >= 100) {
    heap_caps_check_integrity_all(true);
    loopCount = 0;
  }
}