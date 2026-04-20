import subprocess
import sys


def run_script(script_name):
    print(f"\n{'='*50}")
    print(f"🚀 STARTING: {script_name}")
    print(f"{'='*50}\n")

    try:
        # sys.executable zorgt ervoor dat hij exact dezelfde Python-omgeving/virtual env gebruikt
        subprocess.run([sys.executable, script_name], check=True)
        print(f"\n✅ SUCCES: {script_name} is netjes afgerond.")

    except subprocess.CalledProcessError as e:
        print(f"\n❌ FOUT: {script_name} is gecrasht met exit code {e.returncode}.")
        sys.exit(
            1
        )  # Stop de hele pijplijn zodat JobBERT niet draait als de extractie faalt


def main():
    print("🌟 === START VDAB DATA PIPELINE === 🌟")

    # Stap 1: Haal nieuwe vacatures op en extract skills via Qwen (LLM)
    run_script("extract_vdab_skills.py")

    # Stap 2: Normaliseer de nieuw gevonden skills via JobBERT
    run_script("normalize_vdab_skills.py")

    print("\n🎉 PIPELINE VOLLEDIG VOLTOOID! De database is up-to-date.")


if __name__ == "__main__":
    main()
