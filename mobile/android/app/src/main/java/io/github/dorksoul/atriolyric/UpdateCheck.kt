package io.github.dorksoul.atriolyric

import android.content.Context
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.os.Build
import java.io.File
import java.security.MessageDigest

/**
 * Checks a downloaded APK before Android's installer is shown: it must be
 * this app, signed with the same key, and newer than what is installed.
 * Android itself refuses an update signed with another key, but it would
 * happily offer to install some *other* app; this keeps the update button
 * from ever doing that.
 */
object UpdateCheck {
    /** Why `apk` must not be installed, or null when it is fine. */
    fun problem(context: Context, apk: File): String? {
        val manager = context.packageManager
        val archive = archiveInfo(manager, apk) ?: return "The downloaded file is not a valid app"
        if (archive.packageName != context.packageName) return "The server sent a different app"
        val installed = installedInfo(manager, context.packageName) ?: return "Could not read the installed app"
        if (versionCode(archive) <= versionCode(installed)) return "The server's app is not newer than this one"
        val installedSigners = currentSigners(installed)
        val archiveSigners = allSigners(archive)
        if (installedSigners.isEmpty() || !archiveSigners.containsAll(installedSigners)) {
            return "The server's app is not signed with this app's key"
        }
        return null
    }

    @Suppress("DEPRECATION")
    private fun flags(): Int =
        if (Build.VERSION.SDK_INT >= 28) PackageManager.GET_SIGNING_CERTIFICATES else PackageManager.GET_SIGNATURES

    @Suppress("DEPRECATION")
    private fun archiveInfo(manager: PackageManager, apk: File): PackageInfo? = try {
        manager.getPackageArchiveInfo(apk.path, flags())
    } catch (_: Exception) {
        null
    }

    @Suppress("DEPRECATION")
    private fun installedInfo(manager: PackageManager, name: String): PackageInfo? = try {
        manager.getPackageInfo(name, flags())
    } catch (_: Exception) {
        null
    }

    @Suppress("DEPRECATION")
    private fun versionCode(info: PackageInfo): Long =
        if (Build.VERSION.SDK_INT >= 28) info.longVersionCode else info.versionCode.toLong()

    private fun digest(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    /** The certificates the installed app is signed with now. */
    @Suppress("DEPRECATION")
    private fun currentSigners(info: PackageInfo): Set<String> {
        if (Build.VERSION.SDK_INT >= 28) {
            val signing = info.signingInfo ?: return emptySet()
            val list = if (signing.hasMultipleSigners()) signing.apkContentsSigners else signing.signingCertificateHistory?.takeLast(1)?.toTypedArray()
            return list?.map { digest(it.toByteArray()) }?.toSet() ?: emptySet()
        }
        return info.signatures?.map { digest(it.toByteArray()) }?.toSet() ?: emptySet()
    }

    /** Every certificate the APK carries, its rotation history included (a
     * key rotated with Android's key rotation still counts as the same key). */
    @Suppress("DEPRECATION")
    private fun allSigners(info: PackageInfo): Set<String> {
        if (Build.VERSION.SDK_INT >= 28) {
            val signing = info.signingInfo ?: return emptySet()
            val list = (signing.apkContentsSigners?.toList() ?: emptyList()) + (signing.signingCertificateHistory?.toList() ?: emptyList())
            return list.map { digest(it.toByteArray()) }.toSet()
        }
        return info.signatures?.map { digest(it.toByteArray()) }?.toSet() ?: emptySet()
    }
}
