sed -i 's/\r$//' deploy/install.sh
chmod +x deploy/install.sh
sudo ./deploy/install.sh --inplace

sudo systemctl restart gddl_wind_alarm gddl_wind_fetch gddl_pv_fetch
sudo systemctl status gddl_wind_alarm gddl_wind_fetch gddl_pv_fetch --no-pager
sudo journalctl -u gddl_wind_alarm -n 30 --no-pager

#systemctl status gddl_alarm gddl_alarm_py gddl_alarm_fetch gddl_alarm_pv --no-pager
#systemctl status gddl_alarm gddl_wind_alarm gddl_wind_fetch gddl_pv_fetch --no-pager
#journalctl -u gddl_wind_fetch -f
##    "enableVideoDevices": false,
#    "event_type": false,
