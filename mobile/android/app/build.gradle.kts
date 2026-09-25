import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Release signing: read ~/.android/voice-keyboard-release.properties if present
// (keys: storeFile, storePassword, keyAlias, keyPassword). Otherwise the release
// build falls back to the debug keystore so it still produces an installable APK.
val releaseProps = Properties().apply {
    val f = file("${System.getProperty("user.home")}/.android/voice-keyboard-release.properties")
    if (f.isFile) f.inputStream().use { load(it) }
}
val hasReleaseKey = releaseProps.getProperty("storeFile")?.let { file(it).isFile } == true

android {
    namespace = "ai.algofly.voicekeyboard"
    compileSdk = 35

    defaultConfig {
        applicationId = "ai.algofly.voicekeyboard"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "1.0.0"
    }

    signingConfigs {
        if (hasReleaseKey) {
            create("release") {
                storeFile = file(releaseProps.getProperty("storeFile"))
                storePassword = releaseProps.getProperty("storePassword")
                keyAlias = releaseProps.getProperty("keyAlias")
                keyPassword = releaseProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = if (hasReleaseKey) signingConfigs.getByName("release")
            else signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        buildConfig = false
    }
}

dependencies {
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
